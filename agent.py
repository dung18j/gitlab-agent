#!/usr/bin/env python3
"""agent-runner: turn GitLab todos into OpenCode runs.

Flow
----
1. poll   GET  /todos?state=pending
2. claim  POST /projects/:id/{issues,merge_requests}/:iid/notes
3. wait   CLAIM_WAIT_SECONDS
4. race   list every claim note on the same target and keep the earliest one;
          only the earliest claimer is allowed to run the request
5. run    opencode run --auto "<prompt>"
6. done   POST /todos/:id/mark_as_done

GitLab is accessed exclusively through the `glab` CLI, and the request itself is
executed by `opencode`. All configuration is read from the environment, loaded
from an optional .env file (ENV_FILE, default /app/.env). See .env.example.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"{_ts()} [INFO ] {message}", flush=True)


def warn(message: str) -> None:
    print(f"{_ts()} [WARN ] {message}", file=sys.stderr, flush=True)


def error(message: str) -> None:
    print(f"{_ts()} [ERROR] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def _load_env_file(path: Path) -> bool:
    """Load a simple KEY=value .env file without overriding real env vars."""
    if not path.is_file():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_set(name: str) -> set[str]:
    """Comma-separated env var as a lower-cased set."""
    return {
        item.strip().lower()
        for item in _env(name).split(",")
        if item.strip()
    }


def _resolve_gitlab_endpoint() -> tuple[str, str]:
    """Return (bare_host, scheme) from GITLAB_API_URL or GITLAB_HOST.

    Accepts a bare host, a host with a scheme, or a full API URL such as
    ``http://10.10.1.1:8080/api/v4``. ``GLAB_API_PROTOCOL`` overrides the scheme.
    """
    scheme = _env("GLAB_API_PROTOCOL").strip().lower() or _env("API_PROTOCOL").strip().lower()

    api_url = _env("GITLAB_API_URL").strip().rstrip("/")
    if api_url:
        for suffix in ("/api/v4", "/api"):
            if api_url.endswith(suffix):
                api_url = api_url[: -len(suffix)]
                break
        if "://" in api_url:
            parsed, _, api_url = api_url.partition("://")
            scheme = scheme or parsed.lower()
        return api_url.rstrip("/"), (scheme or "https")

    host = _env("GITLAB_HOST", "gitlab.com").strip().rstrip("/")
    if "://" in host:
        parsed, _, host = host.partition("://")
        scheme = scheme or parsed.lower()
    return host, (scheme or "https")


class Config:
    def __init__(self) -> None:
        self.gitlab_host, self.gitlab_protocol = _resolve_gitlab_endpoint()
        self.gitlab_token = _env("GITLAB_TOKEN") or _env("GITLAB_PAT")

        self.poll_interval = _env_int("POLL_INTERVAL", 30)
        self.claim_wait_seconds = _env_int("CLAIM_WAIT_SECONDS", 5)
        self.claim_marker = _env("CLAIM_MARKER", "opencode-agent-claim")
        reaction = _env("CLAIM_REACTION", "eyes").strip()
        self.claim_reaction = (
            "" if reaction.lower() in {"", "none", "false", "off", "no"} else reaction
        )
        self.agent_name = _env("AGENT_NAME", "opencode-agent")
        self.claim_message = _env("CLAIM_MESSAGE") or (
            f"🤖 {self.agent_name} is claiming this task"
        )

        self.workdir = Path(_env("WORKDIR", "/workspace"))

        self.default_action = _env("DEFAULT_ACTION", "implement")
        self.agent_actions = set(
            _env(
                "AGENT_ACTIONS",
                "implement explain review plan fix test refactor document analyze describe",
            ).split()
        )
        self.todo_actions = {
            item.strip()
            for item in _env("TODO_ACTIONS").split(",")
            if item.strip()
        }
        # Only handle todos from these projects / requesters (empty = all).
        self.allowed_projects = _env_set("ALLOWED_PROJECTS")
        self.allowed_requesters = _env_set("ALLOWED_REQUESTERS")

        self.opencode_bin = _env("OPENCODE_BIN", "opencode")
        self.opencode_model = _env("OPENCODE_MODEL")
        self.opencode_agent = _env("OPENCODE_AGENT")
        self.opencode_args = shlex.split(_env("OPENCODE_ARGS"))
        self.opencode_timeout = _env_int("OPENCODE_TIMEOUT", 0)
        # The agent posts its own progress and final reply on GitLab; set
        # POST_RESULT=true to also have the runner post opencode's raw output.
        self.post_result = _env_bool("POST_RESULT", False)
        self.result_max_chars = _env_int("RESULT_MAX_CHARS", 0)

        self.clone_repo = _env_bool("CLONE_REPO", False)
        self.clone_dir = _env("CLONE_DIR")

        self.mr_branch_prefix = _env("MR_BRANCH_PREFIX", "agent/")
        self.mr_target_branch = _env("MR_TARGET_BRANCH")

        self.git_author_name = _env("GIT_AUTHOR_NAME", self.agent_name)
        self.git_author_email = _env("GIT_AUTHOR_EMAIL", f"{self.agent_name}@localhost")

        self.dry_run = _env_bool("DRY_RUN", False)
        self.reply_marker = _env("REPLY_MARKER", f"🤖 {self.agent_name}")
        self.extra_prompt = _env("EXTRA_PROMPT")


def load_config() -> Config:
    candidates = [
        Path(_env("ENV_FILE", "/app/.env")),
        Path("/workspace/.env"),
        Path(__file__).resolve().parent / ".env",
    ]
    for candidate in candidates:
        if _load_env_file(candidate):
            log(f"loading configuration from {candidate}")
            break
    return Config()


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str], *, cwd: Path | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class GlabError(RuntimeError):
    pass


def _parse_json_values(text: str) -> list[Any]:
    """Decode one or more JSON documents from glab stdout.

    Newer glab merges paginated responses into a single array; older versions
    (e.g. 1.53 in Debian trixie) emit each page's JSON back to back. Decoding
    with ``raw_decode`` in a loop handles both.
    """
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        value, index = decoder.raw_decode(text, index)
        values.append(value)
    return values


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def glab_api(
    endpoint: str,
    *,
    method: str | None = None,
    fields: dict[str, str] | None = None,
    stdin_field: str | None = None,
    stdin_value: str | None = None,
    paginate: bool = False,
    silent: bool = False,
) -> Any:
    """Call `glab api` and return the decoded response (or None when silent).

    A large value (e.g. a note body) can be sent with ``stdin_field``, which
    passes ``-F <field>=@-`` and feeds ``stdin_value`` on stdin. This avoids the
    ``Argument list too long`` error from putting big bodies in argv.

    Deliberately avoids ``--hostname`` (its validator rejects ``host:port``) and
    ``--output`` (older glab) — the host and protocol come from GITLAB_HOST.
    """
    cmd = ["glab", "api"]
    if method:
        cmd += ["-X", method]
    if paginate:
        cmd.append("--paginate")
    if silent:
        cmd.append("--silent")
    cmd.append(endpoint)
    for key, value in (fields or {}).items():
        cmd += ["-f", f"{key}={value}"]
    if stdin_field is not None:
        cmd += ["-F", f"{stdin_field}=@-"]

    proc = _run(cmd, input_text=stdin_value)
    if proc.returncode != 0:
        raise GlabError(proc.stderr.strip() or f"glab exited with {proc.returncode}")
    if silent:
        return None

    values = _parse_json_values(proc.stdout)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    # Multiple JSON documents: flatten page arrays, otherwise keep the list.
    if all(isinstance(value, list) for value in values):
        merged: list[Any] = []
        for value in values:
            merged.extend(value)
        return merged
    return values


def configure_git() -> None:
    """Set the commit author/committer identity used by opencode."""
    for key, value in (
        ("user.name", config.git_author_name),
        ("user.email", config.git_author_email),
    ):
        proc = _run(["git", "config", "--global", key, value])
        if proc.returncode != 0:
            warn(f"git config {key} failed: {(proc.stderr or proc.stdout).strip()}")
    os.environ["GIT_AUTHOR_NAME"] = config.git_author_name
    os.environ["GIT_AUTHOR_EMAIL"] = config.git_author_email
    os.environ["GIT_COMMITTER_NAME"] = config.git_author_name
    os.environ["GIT_COMMITTER_EMAIL"] = config.git_author_email


def configure_glab() -> None:
    """Pin the API/git protocol for the host so plain-HTTP instances work.

    Old glab (e.g. 1.53) does not reliably read the protocol from the
    environment, so write it to the per-host config (which lands in the global
    config file when ``-h`` is used).
    """
    for key in ("api_protocol", "git_protocol"):
        proc = _run(
            ["glab", "config", "set", key, config.gitlab_protocol, "-h", config.gitlab_host]
        )
        if proc.returncode != 0:
            warn(
                f"glab config set {key} for {config.gitlab_host} failed: "
                f"{(proc.stderr or proc.stdout).strip()}"
            )


def get_pending_todos() -> list[dict[str, Any]]:
    todos = _as_list(
        glab_api("todos?state=pending&per_page=100", paginate=True)
    )
    return [todo for todo in todos if todo.get("state") == "pending"]


def post_note(plural: str, project_id: Any, iid: Any, body: str) -> int:
    # Send the body on stdin so large notes never hit the argv size limit.
    response = glab_api(
        f"projects/{project_id}/{plural}/{iid}/notes",
        method="POST",
        stdin_field="body",
        stdin_value=body,
    )
    return int(response["id"])


def list_notes(plural: str, project_id: Any, iid: Any) -> list[dict[str, Any]]:
    return _as_list(
        glab_api(
            f"projects/{project_id}/{plural}/{iid}/notes?per_page=100",
            paginate=True,
        )
    )


def derive_request_id(
    notes: list[dict[str, Any]],
    *,
    target_url: str,
    body: str,
    author_id: str,
    todo_id: Any,
) -> str:
    """Identify the specific request (comment) behind a todo.

    Prefers the note id in ``target_url``, then the note whose body and author
    match the todo, and finally falls back to the todo id so distinct requests
    on the same issue/MR never share an id.
    """
    match = re.search(r"[#/]note_(\d+)", target_url or "")
    if match:
        return f"note_{match.group(1)}"

    wanted = (body or "").strip()
    if wanted:
        matches = [
            note
            for note in notes
            if not note.get("system")
            and (note.get("body") or "").strip() == wanted
            and (
                not author_id
                or str((note.get("author") or {}).get("id")) == str(author_id)
            )
        ]
        if matches:
            matches.sort(key=lambda note: note.get("id", 0), reverse=True)
            return f"note_{matches[0]['id']}"

    return f"todo_{todo_id}"


def build_claim_message(request_id: str) -> str:
    base = config.claim_message or f"🤖 {config.agent_name} is claiming this task"
    return (
        f"{base} (request {request_id}) "
        f"<!-- {config.claim_marker} request={request_id} -->"
    )


def _is_claim_for(note: dict[str, Any], request_id: str) -> bool:
    text = note.get("body") or ""
    return config.claim_marker in text and f"request={request_id}" in text


def find_claims(
    notes: list[dict[str, Any]], request_id: str
) -> list[dict[str, Any]]:
    claims = [note for note in notes if _is_claim_for(note, request_id)]
    claims.sort(key=lambda note: (note.get("created_at", ""), note.get("id", 0)))
    return claims


def mark_todo_done(todo_id: Any) -> None:
    try:
        glab_api(f"todos/{todo_id}/mark_as_done", method="POST", silent=True)
    except GlabError as exc:
        warn(f"could not mark todo {todo_id} as done: {exc}")


def _reaction_endpoint(plural: str, project_id: Any, iid: Any, request_id: str) -> str:
    """Award-emoji endpoint for the note when the request is a note, else the issue/MR."""
    base = f"projects/{project_id}/{plural}/{iid}"
    match = re.match(r"note_(\d+)$", request_id or "")
    if match:
        return f"{base}/notes/{match.group(1)}/award_emoji"
    return f"{base}/award_emoji"


def add_claim_reaction(plural: str, project_id: Any, iid: Any, request_id: str) -> bool:
    """React with the claim emoji as a visible marker on the request."""
    if not config.claim_reaction:
        return False
    endpoint = _reaction_endpoint(plural, project_id, iid, request_id)
    name = quote(config.claim_reaction, safe="")
    try:
        glab_api(f"{endpoint}?name={name}", method="POST")
        return True
    except GlabError as exc:
        warn(f"could not add '{config.claim_reaction}' reaction: {exc}")
        return False


# ---------------------------------------------------------------------------
# request parsing and prompt
# ---------------------------------------------------------------------------


def parse_request(body: str, title: str) -> tuple[str, str]:
    """Return (action, instruction) from a todo body."""
    cleaned = re.sub(r"^(?:\s*@[A-Za-z0-9_.-]+\s+)+", "", body).strip()
    first = cleaned.split()[0].lower().lstrip("/") if cleaned else ""

    if first in config.agent_actions:
        action = first
        instruction = re.sub(
            rf"^\s*/?{re.escape(action)}\s*:?\s*",
            "",
            cleaned,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        action = config.default_action
        instruction = body

    if not instruction.strip():
        instruction = title or "Proceed with the task described by this reference."
    return action, instruction


INTRO = """You are an autonomous software-engineering agent. You were triggered by a \
request on GitLab. Work unattended: do not ask for confirmation, gather what \
you need, do the work, and reply on GitLab.

"""

HOW_TO_WORK = """
## How to work
1. The project is already cloned into the current working directory; work there.
   The checkout's remote is authenticated, so `git fetch` and `git push` work as
   is. If the directory is not a git checkout, clone it from the Repository URL
   in the Trigger section.
2. Read the full context with the `glab` CLI. It is already authenticated from
   GITLAB_HOST/GITLAB_TOKEN, so never run `glab auth login`. Pass `-R <project>`
   (from the Trigger section) to commands that accept it:
   - Issue and its comments: `glab issue view <iid> -R <project> --comments`
   - Merge request and its comments: `glab mr view <iid> -R <project> --comments`
   - Merge request diff: `glab mr diff <iid> -R <project> --raw --color=never`
   - Anything else: `glab api <endpoint>` hits the REST API. Endpoints are
     relative to /api/v4; use the numeric project_id above and prefer
     `per_page=100` over `--paginate`.
3. Detect the task type below and follow the matching playbook.
4. Keep the thread updated as you go, then post the result back on GitLab.
"""

RULES = """
## Rules
- Never merge a merge request. Never delete branches, tags, files, or projects.
- Never push directly to the default branch. All code changes go through a new
  branch plus a merge request.
- Stay within the scope of the request. Do not refactor or reformat unrelated code.
- Match the repository's existing style, conventions, and test framework.
- Use `glab` for all GitLab access. It is pre-authenticated from the environment;
  never run `glab auth login`.
- Never print, commit, or echo secrets (tokens, credentials, private keys).
- If the request is ambiguous, state your assumption and proceed. Only ask a
  question via a GitLab reply if you genuinely cannot proceed safely.
- Keep the GitLab reply concise and skimmable: short summary first, then details.
  Use Markdown, code fences, and `path:line` references.
- If you open a branch or merge request, link it in the reply.
"""

PROGRESS = """
## Progress updates
Keep the request informed while you work; do not wait until the very end. Post a
short update back on the thread (same command as the final reply) when you:
- start — restate the request in one line and outline your plan,
- finish gathering context,
- reach a meaningful milestone (change made, files touched, branch pushed),
- start and finish tests, or
- get blocked or change approach.
Keep each update to a few lines and prefix it with the marker `{marker}` on its
own line, exactly like the final reply. Do not narrate every command; a handful
of updates over the whole run is the goal. If the task is quick, one combined
update at the end is fine.
"""

PLAYBOOKS = """
## Playbooks

### Code review
Goal: assess the merge request and report findings. Do not change code unless asked.
1. Read the MR description and the full discussion.
2. List changed files, then fetch diffs for the relevant files.
3. Check, in priority order: correctness and logic bugs; security issues
   (injection, authz, secrets, unsafe deserialization); data loss or migration
   risk; concurrency; error handling; performance; API/DB compatibility;
   missing or weak tests; style and naming.
4. Produce a prioritised list. For each finding give: severity
   (blocker / major / minor / nit), `path:line`, what is wrong, and a concrete fix.
5. Post one summary note. For blocking findings you may also open inline threads.
6. Do not approve or merge unless the request explicitly says to.

### Implementation
Goal: implement the requested change and open a merge request.
1. Read the issue/MR and all discussion to capture the requirements.
2. Explore the code to find where the change belongs and how similar code is
   written and tested. Work in the current checkout.
3. Create a branch from the default branch (a suggested name is in the Trigger
   section).
4. Make the smallest coherent change. Update docs and the changelog if the
   project maintains them. Add or update tests.
5. Commit and push the branch, then open a merge request targeting the default
   branch, for example:
   `glab mr create -R <project> --source-branch <branch> --target-branch <default> --fill --yes`
   In the description: summarise the change, list the tests you ran, and reference
   the source (`Closes #<iid>` for issues). Mark it as a draft if it is incomplete.
6. Reply on the original thread with the merge request link and a short summary.

### Explanation
Goal: answer a question. Make no edits.
1. Read the referenced code with `glab` and the local file/search tools.
2. Explain clearly and concisely, with `path:line` references and small excerpts.
   Call out assumptions and edge cases.
3. Post the explanation as a note on this thread.

### Testing
Goal: add or fix tests for the described behaviour.
1. Identify the existing test framework and conventions.
2. Add focused tests (happy path and edge cases); keep them deterministic.
3. If CI is available, trigger or observe it (`glab ci list -R <project>` shows
   pipelines); otherwise say the tests were not run. Prefer a branch plus merge
   request over pushing anywhere.
4. Reply with what you added and the result.

### Refactoring
Goal: improve structure without changing behaviour.
1. Make sure tests cover the area first; add them if missing.
2. Refactor incrementally so the diff stays reviewable.
3. Open a branch plus merge request and explain the motivation and the safety net.

### General
1. Treat the requested action text as the instruction.
2. Gather the needed context with `glab` and the local tools.
3. Do the minimum necessary, then reply with what you did, what you found, and any
   follow-up you recommend.
"""


def classify_request(text: str) -> str:
    lowered = text.lower()

    def has(keys: list[str]) -> bool:
        return any(key in lowered for key in keys)

    if has(["review", "reviewing", "code review", "pr review", "審査", "レビュー", "评审", "代码审查"]):
        return "code review"
    if has(["explain", "explanation", "what does", "what is", "why", "how does", "how do",
            "walk me through", "describe", "説明", "解説", "解释", "解釋", "说明"]):
        return "explanation"
    if has(["test", "tests", "testing", "unit test", "テスト", "测试", "測試"]):
        return "testing"
    if has(["refactor", "refactoring", "clean up", "restructure", "リファクタ", "重构", "重構"]):
        return "refactoring"
    if has(["implement", "add", "create", "build", "write", "fix", "support", "introduce",
            "update", "change", "rename", "remove", "migrate", "実装", "追加", "修正", "対応",
            "实现", "實現", "新增", "修复", "修改"]):
        return "implementation"
    return "general"


def _repo_web_url(project: str) -> str:
    return f"{config.gitlab_protocol}://{config.gitlab_host}/{project}.git"


def build_prompt(
    *,
    action: str,
    instruction: str,
    title: str,
    url: str,
    project: str,
    project_id: Any,
    type_: str,
    iid: Any,
    todo_id: Any,
    request_id: str,
    author: str,
    body: str,
) -> str:
    """One prompt for every task type: trigger context plus all playbooks."""
    request_text = (instruction or body).strip()
    task = classify_request(f"{action} {request_text}")
    glab_target = "issue" if type_ == "issue" else "mr"
    mark = "#" if type_ == "issue" else "!"
    branch = f"{config.mr_branch_prefix}{request_id}"

    parts: list[str] = [INTRO]
    parts.append("## Trigger\n")
    parts.append(f"- Project: {project} (project_id: {project_id})\n")
    parts.append(f"- Repository: {_repo_web_url(project)}\n")
    parts.append(f"- Target: {type_} {mark}{iid} — {title}\n")
    parts.append(f"- Request id: {request_id}\n")
    parts.append(f"- Requested by: @{author or 'unknown'}\n")
    parts.append(f"- Thread: {url or '(unknown)'}\n")
    parts.append(f"- Suggested branch: {branch}\n")
    parts.append(
        f"- MR target branch: {config.mr_target_branch or '(project default)'}\n"
    )
    parts.append("\n### Raw trigger text\n```\n")
    parts.append((body or "").strip())
    parts.append("\n```\n")

    parts.append("\n### Requested action\n")
    if request_text:
        parts.append("```\n")
        parts.append(request_text)
        parts.append("\n```\n")
    else:
        parts.append("(The request body is empty — infer the task from the context above.)\n")
    parts.append(f"\nDetected task type: **{task}**. Follow the matching playbook below.\n")

    parts.append(HOW_TO_WORK)
    parts.append(RULES)
    parts.append(PROGRESS.format(marker=config.reply_marker))
    parts.append(PLAYBOOKS)

    parts.append("\n## Reply target for this trigger\n")
    parts.append(
        f'- Post your progress updates and final reply with: `glab {glab_target} note '
        f'{iid} -R {project} -m "<markdown>"`\n'
    )
    parts.append(
        f"- Begin every note (progress update and final reply) with the marker "
        f"`{config.reply_marker}` on its own line.\n"
    )
    parts.append(
        f"- Address the requester as @{author or 'unknown'} and reference the "
        "repository paths and line numbers you used.\n"
    )

    if config.extra_prompt.strip():
        parts.append("\n## Additional instructions\n")
        parts.append(config.extra_prompt.strip())
        parts.append("\n")

    parts.append("\nBegin now.\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


def _clone_url(project_path: str) -> str:
    """HTTPS clone URL with the PAT as userinfo (no SSH keys needed)."""
    host = config.gitlab_host
    scheme = config.gitlab_protocol
    if config.gitlab_token:
        token = quote(config.gitlab_token, safe="")
        return f"{scheme}://oauth2:{token}@{host}/{project_path}.git"
    return f"{scheme}://{host}/{project_path}.git"


def ensure_clone(project_path: str, dest: Path) -> bool:
    url = _clone_url(project_path)

    if (dest / ".git").is_dir():
        log(f"updating existing clone in {dest}")
        _run(["git", "-C", str(dest), "remote", "set-url", "origin", url])
        proc = _run(["git", "-C", str(dest), "fetch", "--all", "--prune", "--quiet"])
        if proc.returncode != 0:
            warn(f"git fetch failed in {dest}: {proc.stderr.strip()}")
            return False
        return True

    log(f"cloning {project_path} into {dest} over https")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    proc = _run(["git", "clone", url, str(dest)])
    if proc.returncode != 0:
        warn(f"git clone {project_path} failed: {proc.stderr.strip()}")
        shutil.rmtree(dest, ignore_errors=True)
        return False
    return True


def resolve_run_dir(project_path: str) -> Path:
    if config.clone_repo and project_path:
        dest = Path(config.clone_dir) if config.clone_dir else config.workdir / project_path
        if ensure_clone(project_path, dest):
            return dest
        warn(f"clone unavailable for {project_path}; falling back to {config.workdir}")
    return config.workdir


# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------


def run_opencode(directory: Path, prompt: str, title: str = "") -> tuple[str, int]:
    cmd = [config.opencode_bin, "run", "--auto"]
    if config.opencode_model:
        cmd += ["--model", config.opencode_model]
    if config.opencode_agent:
        cmd += ["--agent", config.opencode_agent]
    if title:
        cmd += ["--title", title]
    cmd += config.opencode_args
    cmd.append(prompt)

    if config.dry_run:
        return "[dry-run] " + " ".join(shlex.quote(part) for part in cmd), 0

    if config.opencode_timeout > 0:
        cmd = ["timeout", str(config.opencode_timeout), *cmd]

    if not directory.is_dir():
        warn(f"run directory {directory} does not exist; using {config.workdir}")
        directory = config.workdir
        directory.mkdir(parents=True, exist_ok=True)

    log(f"running {' '.join(shlex.quote(part) for part in cmd[:3])} in {directory}")
    proc = _run(cmd, cwd=directory)
    return (proc.stdout + proc.stderr), proc.returncode


def _fence_for(text: str) -> str:
    """A backtick fence longer than any run of backticks in ``text``."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def export_session(directory: Path, title: str) -> str | None:
    """Export the sanitized JSON transcript of the matching opencode session."""
    try:
        listed = _run(
            [config.opencode_bin, "session", "list", "--format", "json", "-n", "20"],
            cwd=directory,
        )
    except OSError as exc:
        warn(f"could not list opencode sessions: {exc}")
        return None
    if listed.returncode != 0:
        warn(f"opencode session list failed: {(listed.stderr or listed.stdout).strip()}")
        return None

    values = _parse_json_values(listed.stdout)
    sessions = values[0] if values and isinstance(values[0], list) else values
    if not isinstance(sessions, list) or not sessions:
        warn("no opencode session found to export")
        return None
    chosen = next(
        (s for s in sessions if isinstance(s, dict) and s.get("title") == title),
        sessions[0],
    )
    session_id = chosen.get("id") if isinstance(chosen, dict) else None
    if not session_id:
        warn("opencode session has no id")
        return None

    # --sanitize redacts secrets from the transcript before we post it.
    cmd = [config.opencode_bin, "session", "export", str(session_id), "--sanitize"]
    proc = _run(cmd, cwd=directory)
    if proc.returncode != 0:
        warn(f"opencode session export failed: {(proc.stderr or proc.stdout).strip()}")
        return None
    return proc.stdout


def build_result_note(directory: Path, title: str) -> str:
    """The note body for POST_RESULT: the sanitized session in a code block."""
    session = export_session(directory, title)
    if not session or not session.strip():
        return ""
    limit = config.result_max_chars
    body = (session if limit <= 0 else session[-limit:]).strip()
    fence = _fence_for(body)
    return (
        "<details><summary>opencode session</summary>\n\n"
        f"{fence}json\n{body}\n{fence}\n\n"
        "</details>"
    )


# ---------------------------------------------------------------------------
# todo processing
# ---------------------------------------------------------------------------


def _in_allowlist(allow: set[str], *values: Any) -> bool:
    """True when the allow-list is empty or any value is in it."""
    if not allow:
        return True
    return any(str(value).lower() in allow for value in values if value not in (None, ""))


def process_todo(todo: dict[str, Any], self_user_id: str | None) -> bool:
    """Handle a single todo. Returns True when a request was actually run."""
    todo_id = todo.get("id")
    if todo_id is None:
        return False

    action_name = todo.get("action_name") or ""
    target_type = todo.get("target_type") or ""
    project = todo.get("project") or {}
    target = todo.get("target") or {}
    project_id = project.get("id")
    project_path = project.get("path_with_namespace") or ""
    title = target.get("title") or ""
    body = todo.get("body") or ""
    author = (todo.get("author") or {}).get("username") or ""
    author_id = str((todo.get("author") or {}).get("id") or "")
    target_url = todo.get("target_url") or ""
    iid = target.get("iid")

    if config.todo_actions and action_name not in config.todo_actions:
        log(f"skip todo {todo_id} (action '{action_name}' not allowed)")
        return False

    if self_user_id and author_id == self_user_id:
        log(f"skip todo {todo_id} (authored by this agent)")
        return False

    if not _in_allowlist(config.allowed_projects, project_path, project_id):
        log(f"skip todo {todo_id} (project '{project_path}' not in ALLOWED_PROJECTS)")
        return False
    if not _in_allowlist(config.allowed_requesters, author, author_id):
        log(f"skip todo {todo_id} (requester '@{author}' not in ALLOWED_REQUESTERS)")
        return False

    plurals = {"Issue": "issues", "MergeRequest": "merge_requests"}
    plural = plurals.get(target_type)
    if plural is None:
        log(f"skip todo {todo_id} (unsupported target_type '{target_type}')")
        mark_todo_done(todo_id)
        return False
    type_label = "issue" if target_type == "Issue" else "merge request"

    # Fall back to the target URL when the API omits structured target data.
    if not iid and target_url:
        match = re.search(r"/-/(?:issues|merge_requests)/(\d+)", target_url)
        if match:
            iid = match.group(1)
    if not project_id and project_path:
        encoded = project_path.replace("/", "%2F")
        project = glab_api(f"projects/{encoded}")
        project_id = (project or {}).get("id")

    if not project_id or not iid:
        warn(f"skip todo {todo_id} (could not resolve project_id={project_id!r} iid={iid!r})")
        mark_todo_done(todo_id)
        return False

    # GitLab is the source of truth: identify this specific request (comment)
    # and skip only when that request already has a claim note.
    try:
        notes = list_notes(plural, project_id, iid)
    except GlabError as exc:
        warn(f"could not read notes for todo {todo_id}: {exc}")
        notes = []
    request_id = derive_request_id(
        notes,
        target_url=target_url,
        body=body,
        author_id=author_id,
        todo_id=todo_id,
    )
    if find_claims(notes, request_id):
        log(f"skip todo {todo_id} (request {request_id} already claimed)")
        mark_todo_done(todo_id)
        return False

    action, instruction = parse_request(body, title)
    prompt = build_prompt(
        action=action,
        instruction=instruction,
        title=title,
        url=target_url,
        project=project_path,
        project_id=project_id,
        type_=type_label,
        iid=iid,
        todo_id=todo_id,
        request_id=request_id,
        author=author,
        body=body,
    )

    if config.dry_run:
        log(
            f"[dry-run] would claim todo {todo_id} ({type_label} !{iid} in "
            f"{project_path}) as request {request_id}, react "
            f"'{config.claim_reaction}', and run opencode action '{action}':\n{prompt}"
        )
        return True

    log(f"claiming todo {todo_id} ({type_label} !{iid}, request {request_id})")

    try:
        our_note = post_note(plural, project_id, iid, build_claim_message(request_id))
    except GlabError as exc:
        warn(f"could not post claim for todo {todo_id}: {exc}")
        return False

    # Visible marker on the request (the note itself when we know it).
    add_claim_reaction(plural, project_id, iid, request_id)

    log(
        f"claim note {our_note} posted; waiting {config.claim_wait_seconds}s "
        "for competing claims"
    )
    interruptible_sleep(config.claim_wait_seconds)

    try:
        claims = find_claims(list_notes(plural, project_id, iid), request_id)
    except GlabError as exc:
        warn(f"could not re-read notes for todo {todo_id}: {exc}")
        claims = []
    winner = int(claims[0]["id"]) if claims else None
    if winner != our_note:
        log(
            f"claim lost for todo {todo_id} (request {request_id}, earliest claim "
            f"note id={winner}, ours={our_note}); yielding"
        )
        mark_todo_done(todo_id)
        return False

    log(f"claim won for todo {todo_id} (note {our_note})")

    run_dir = resolve_run_dir(project_path)
    session_title = f"{config.agent_name}: {title or type_label} (todo {todo_id})"

    output, returncode = run_opencode(run_dir, prompt, session_title)
    log(f"opencode finished for todo {todo_id} with exit code {returncode}")

    if config.post_result:
        note = build_result_note(run_dir, session_title)
        if note:
            try:
                post_note(plural, project_id, iid, note)
            except GlabError as exc:
                warn(f"could not post result for todo {todo_id}: {exc}")
        else:
            log(f"no session export to post for todo {todo_id}")

    mark_todo_done(todo_id)
    return True


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

stopping = False


def _handle_signal(_signum: int, _frame: Any) -> None:
    global stopping
    log("signal received, stopping")
    stopping = True


def interruptible_sleep(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while not stopping and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def main() -> None:
    global config

    # Normalise the host and tell glab which protocol to use. glab derives both
    # the host and the protocol from a fully-qualified GITLAB_HOST URL (and its
    # --hostname validator rejects "host:port", so we never pass --hostname).
    os.environ["GITLAB_HOST"] = f"{config.gitlab_protocol}://{config.gitlab_host}"
    for name in ("API_PROTOCOL", "GLAB_API_PROTOCOL", "GIT_PROTOCOL", "GLAB_GIT_PROTOCOL"):
        os.environ[name] = config.gitlab_protocol
    if config.gitlab_token:
        os.environ["GITLAB_TOKEN"] = config.gitlab_token
    # Never block waiting for an interactive credential prompt.
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")

    configure_glab()
    configure_git()

    log(
        f"agent-runner starting (host={config.gitlab_protocol}://{config.gitlab_host}, "
        f"poll={config.poll_interval}s, claim_wait={config.claim_wait_seconds}s)"
    )
    log(
        f"model='{config.opencode_model or '<default>'}' workdir={config.workdir} "
        f"clone_repo={config.clone_repo} dry_run={config.dry_run}"
    )
    if not config.clone_repo:
        warn("CLONE_REPO=false: opencode has no checkout to change or open an MR from")

    self_user_id: str | None = None
    try:
        user = glab_api("user")
        self_user_id = str((user or {}).get("id") or "") or None
    except GlabError as exc:
        warn(f"could not resolve the authenticated GitLab user: {exc}")
    if self_user_id:
        log(f"authenticated as GitLab user id {self_user_id}")

    while not stopping:
        try:
            todos = get_pending_todos()
        except GlabError as exc:
            warn(f"listing todos failed: {exc}")
            interruptible_sleep(config.poll_interval)
            continue

        if not todos:
            log(f"no pending todos; sleeping {config.poll_interval}s")
            interruptible_sleep(config.poll_interval)
            continue

        acted = False
        for todo in todos:
            if stopping:
                break
            if process_todo(todo, self_user_id):
                acted = True
                break
        if not acted:
            interruptible_sleep(config.poll_interval)

    log("agent-runner stopped")


def check_requirements() -> None:
    for binary in ("glab", "git", config.opencode_bin):
        if shutil.which(binary) is None:
            error(f"required command not found: {binary}")
            sys.exit(1)
    if not config.gitlab_token:
        error("GITLAB_TOKEN (or GITLAB_PAT) must be set")
        sys.exit(1)
    config.workdir.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    config = load_config()
    check_requirements()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    main()
