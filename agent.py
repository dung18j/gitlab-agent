#!/usr/bin/env python3
"""gitlab-agent — work the GitLab to-do list with OpenCode.

Runs inside the gitlab-agent container. Discovery is the authenticated user's
pending GitLab to-dos (mentions and directly-addressed comments) rather than a
keyword scan. It uses the `glab` CLI for all GitLab access (discovery, the 👀
claim reaction, marking the to-do done) and calls `opencode run` directly;
OpenCode posts the reply through `glab` as well.

Configure it through the environment, normally `podman run --env-file .env ...`;
see README.md and .env.example.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

DEFAULT_MARKER = "🤖"
DEFAULT_EMOJI = "eyes"
DEFAULT_WORKDIR = "/workspace"
DEFAULT_INTERVAL = 60
DEFAULT_TIMEOUT = 3600
DEFAULT_MAX = 1

# To-do actions that carry the request text in `body`. Other actions (assigned,
# review_requested, ...) have no comment body and are ignored.
ACTIONS = ("mentioned", "directly_addressed")

_NOTE_ANCHOR = re.compile(r"#note_(\d+)")


# --------------------------------------------------------------------------- #
# glab plumbing
# --------------------------------------------------------------------------- #

def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}", flush=True)


class GlabError(RuntimeError):
    pass


def glab(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["glab", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GlabError(f"glab {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc.stdout


def glab_api(endpoint: str):
    """GET a GitLab API endpoint and decode the JSON (object or array)."""
    out = glab("api", endpoint, "--output", "json").strip()
    return json.loads(out) if out else None


def glab_post(endpoint: str, fields: dict[str, str]) -> None:
    args = ["api", endpoint, "--method", "POST"]
    for key, value in fields.items():
        args += ["--field", f"{key}={value}"]
    args.append("--silent")
    glab(*args)


def quote_ref(ref: str) -> str:
    """Percent-encode a project path so `group/sub/project` is one segment."""
    return urllib.parse.quote(ref.strip().strip("/"), safe="")


def setup_environment() -> None:
    """Make `glab` (and OpenCode) work from the same .env the bot reads.

    `GITLAB_API_URL` is the REST endpoint (e.g. https://gitlab.com/api/v4);
    `glab` wants the instance URL plus an explicit protocol.
    """
    api_url = (os.environ.get("GITLAB_API_URL") or "").strip().rstrip("/")
    if api_url:
        host = api_url
        for suffix in ("/api/v4", "/api"):
            if host.endswith(suffix):
                host = host[: -len(suffix)]
                break
        host = host.rstrip("/")
        scheme = "http" if api_url.split("://", 1)[0].lower() == "http" else "https"
        os.environ["GITLAB_HOST"] = host
        # Old glab reads API_PROTOCOL/GIT_PROTOCOL, newer ones also accept the
        # GLAB_ names. Set both so either version behaves the same.
        os.environ["API_PROTOCOL"] = scheme
        os.environ["GLAB_API_PROTOCOL"] = scheme
        os.environ["GIT_PROTOCOL"] = scheme
        os.environ["GLAB_GIT_PROTOCOL"] = scheme

    token = os.environ.get("GITLAB_PERSONAL_ACCESS_TOKEN") or os.environ.get("GITLAB_TOKEN")
    if token:
        os.environ["GITLAB_TOKEN"] = token


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    projects: list[str]
    authors: list[str]
    model: Optional[str]
    interval: int
    timeout: int
    workdir: str
    emoji: str
    reply_marker: str
    max_triggers: int
    ignore_self: bool
    once: bool
    dry_run: bool
    auto: bool
    mark_todo: bool = True
    extra_prompt: str = ""


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def split_list(raw: str) -> list[str]:
    parts = raw.replace(";", ",").replace("\n", ",")
    out = []
    for item in parts.replace(" ", ",").split(","):
        item = item.strip().strip("@").strip("/")
        if item:
            out.append(item)
    return out


def parse_args(argv: list[str]) -> Config:
    parser = argparse.ArgumentParser(
        prog="gitlab-agent",
        description="Work the GitLab to-do list with OpenCode.",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        metavar="ID|PATH",
        help="Only handle to-dos from this project (repeatable, comma-separated)",
    )
    parser.add_argument(
        "--author",
        action="append",
        default=[],
        metavar="USERNAME",
        help="Only handle to-dos authored by this GitLab user (repeatable, "
        "comma-separated). Empty = any author.",
    )
    parser.add_argument("--model", default=env("OPENCODE_MODEL"))
    parser.add_argument(
        "--interval",
        type=int,
        default=int(env("INTERVAL", str(DEFAULT_INTERVAL))),
        help="Seconds between sweeps (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(env("TIMEOUT", str(DEFAULT_TIMEOUT))),
        help="Kill an OpenCode run after this many seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--workdir",
        default=env("WORKSPACE", DEFAULT_WORKDIR),
        help="Directory OpenCode runs in (default: %(default)s)",
    )
    parser.add_argument(
        "--emoji",
        default=env("EMOJI", DEFAULT_EMOJI),
        help="Reaction used as the handled marker (default: %(default)s)",
    )
    parser.add_argument(
        "--reply-marker",
        default=env("REPLY_MARKER", DEFAULT_MARKER),
        help="First line of the agent's reply (default: %(default)s)",
    )
    parser.add_argument(
        "--extra-prompt",
        default=env("EXTRA_PROMPT", ""),
        help="Extra instructions appended to every prompt (default: none)",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=int(env("MAX", str(DEFAULT_MAX))),
        help="To-dos handled per sweep (default: %(default)s)",
    )
    parser.add_argument(
        "--ignore-self",
        action=argparse.BooleanOptionalAction,
        default=env_bool("IGNORE_SELF", True),
        help="Ignore to-dos authored by the bot account (default: %(default)s). "
        "The agent's own replies are always ignored via the reply marker.",
    )
    parser.add_argument(
        "--mark-todo",
        action=argparse.BooleanOptionalAction,
        default=env_bool("MARK_TODO", True),
        help="Mark the to-do done after handling it (default: %(default)s)",
    )
    parser.add_argument("--once", action="store_true", help="Run one sweep and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print prompts, run nothing")
    parser.add_argument("--no-auto", action="store_true", help="Do not pass --auto to `opencode run`")
    args = parser.parse_args(argv)

    projects: list[str] = []
    for value in args.project:
        projects += split_list(value)
    projects += split_list(env("PROJECTS", "") or "")
    seen: set[str] = set()
    unique_projects = []
    for project in projects:
        if project not in seen:
            seen.add(project)
            unique_projects.append(project)

    authors: list[str] = []
    for value in args.author:
        authors += split_list(value)
    authors += split_list(env("AUTHORS", "") or "")
    seen_authors: set[str] = set()
    unique_authors = []
    for author in authors:
        key = author.lower()
        if key not in seen_authors:
            seen_authors.add(key)
            unique_authors.append(key)

    return Config(
        projects=unique_projects,
        authors=unique_authors,
        model=args.model,
        interval=max(1, args.interval),
        timeout=max(1, args.timeout),
        workdir=args.workdir,
        emoji=args.emoji,
        reply_marker=args.reply_marker,
        max_triggers=max(1, args.max),
        ignore_self=args.ignore_self,
        once=args.once,
        dry_run=args.dry_run,
        auto=not args.no_auto,
        mark_todo=args.mark_todo,
        # Accept `\n` in the env value as a newline, since env files are
        # usually single-line.
        extra_prompt=args.extra_prompt.replace("\\n", "\n"),
    )


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    project_path: str
    project_id: int
    kind: str  # "issue" | "mr"
    iid: int
    title: str
    note_id: Optional[int]
    author: str
    body: str
    request: str
    web_url: str
    updated_at: str
    todo_id: Optional[int] = None

    def key(self) -> str:
        return f"{self.project_id}:{self.kind}:{self.iid}:{self.note_id or 'description'}"

    def awardable(self) -> str:
        segment = "issues" if self.kind == "issue" else "merge_requests"
        if self.note_id is not None:
            return f"projects/{self.project_id}/{segment}/{self.iid}/notes/{self.note_id}/award_emoji"
        return f"projects/{self.project_id}/{segment}/{self.iid}/award_emoji"


def resolve_projects(cfg: Config) -> list[dict]:
    resolved = []
    for ref in cfg.projects:
        try:
            project = glab_api(f"projects/{quote_ref(ref)}") or {}
        except GlabError as error:
            log(f"cannot resolve project '{ref}': {error}")
            continue
        if project.get("id"):
            resolved.append(
                {"id": project["id"], "path": project.get("path_with_namespace", ref)}
            )
        else:
            log(f"cannot resolve project '{ref}'; skipping")
    return resolved


def note_id_from_url(url: str) -> Optional[int]:
    match = _NOTE_ANCHOR.search(url or "")
    return int(match.group(1)) if match else None


def discover_todos(cfg: Config, me_id: int, allowed_ids: Optional[set[int]]) -> list[Candidate]:
    todos = glab_api("todos?state=pending&per_page=100") or []
    found: list[Candidate] = []

    for todo in todos:
        if todo.get("action_name") not in ACTIONS:
            continue

        body = todo.get("body") or ""
        if not body:
            continue
        # The agent's own replies mention the account and would otherwise loop.
        if cfg.reply_marker and cfg.reply_marker in body:
            continue

        project = todo.get("project") or {}
        target = todo.get("target") or {}
        project_id = project.get("id")
        iid = target.get("iid")
        kind = {"Issue": "issue", "MergeRequest": "mr"}.get(todo.get("target_type"))
        if not project_id or not iid or not kind:
            continue
        if allowed_ids is not None and project_id not in allowed_ids:
            continue

        author = todo.get("author") or {}
        if cfg.ignore_self and author.get("id") == me_id:
            continue
        username = (author.get("username") or "").lower()
        if cfg.authors and username not in cfg.authors:
            continue

        found.append(
            Candidate(
                project_path=project.get("path_with_namespace") or "",
                project_id=project_id,
                kind=kind,
                iid=iid,
                title=target.get("title") or "",
                note_id=note_id_from_url(todo.get("target_url") or ""),
                author=author.get("username") or "unknown",
                body=body,
                request=body.strip(),
                web_url=todo.get("target_url") or target.get("web_url") or "",
                updated_at=todo.get("updated_at") or todo.get("created_at") or "",
                todo_id=todo.get("id"),
            )
        )
    return found


# --------------------------------------------------------------------------- #
# reactions and to-do bookkeeping
# --------------------------------------------------------------------------- #

def already_reacted(awardable: str, emoji: str, me_id: int) -> bool:
    emojis = glab_api(awardable) or []
    return any(
        e.get("name") == emoji and (e.get("user") or {}).get("id") == me_id
        for e in emojis
    )


def react(awardable: str, emoji: str) -> None:
    glab_post(awardable, {"name": emoji})


def mark_done(cfg: Config, candidate: Candidate) -> None:
    if cfg.dry_run or not cfg.mark_todo or candidate.todo_id is None:
        return
    try:
        glab("todo", "done", str(candidate.todo_id))
    except GlabError as error:
        log(f"could not mark to-do {candidate.todo_id} done: {error}")


# --------------------------------------------------------------------------- #
# prompt
# --------------------------------------------------------------------------- #

def classify(request: str) -> str:
    text = request.lower()
    has = lambda keys: any(k in text for k in keys)  # noqa: E731

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


def target_label(candidate: Candidate) -> str:
    if candidate.kind == "issue":
        return f"issue #{candidate.iid} — {candidate.title}"
    return f"merge request !{candidate.iid} — {candidate.title}"


def trigger_label(candidate: Candidate) -> str:
    if candidate.note_id is not None:
        return f"comment/reply (note {candidate.note_id})"
    return f"{'issue' if candidate.kind == 'issue' else 'merge request'} description"


def clone_url(project_path: str) -> str:
    host = (os.environ.get("GITLAB_HOST") or "https://gitlab.com").rstrip("/")
    return f"{host}/{project_path.strip('/')}.git"


INTRO = """You are an autonomous software-engineering agent. You were triggered by a \
request on GitLab. Work unattended: do not ask for confirmation, gather what \
you need, do the work, and reply on GitLab.

"""

HOW_TO_WORK = """
## How to work
1. Read the full context with the `glab` CLI. It is already authenticated from
   GITLAB_HOST/GITLAB_TOKEN, so never run `glab auth login`. Pass
   `-R <project path>` (from the Trigger section) to commands that accept it:
   - Issue and its comments: `glab issue view <iid> -R <project> --comments`
   - Merge request and its comments: `glab mr view <iid> -R <project> --comments`
   - Merge request diff: `glab mr diff <iid> -R <project> --raw --color=never`
   - Anything else: `glab api <endpoint> --output json` hits the REST API.
     Endpoints are relative to /api/v4; use the numeric project_id above.
     Prefer `per_page=100` over `--paginate` (page links from some self-hosted
     instances drop a non-standard port). For example:
       glab api "projects/<project_id>/merge_requests/<iid>/notes?per_page=100" --output json
       glab api "projects/<project_id>/issues/<iid>/notes?per_page=100" --output json
       glab api "projects/<project_id>/repository/tree?ref=<ref>&recursive=true&per_page=100" --output json
       glab api "projects/<project_id>/repository/files/<url-encoded-path>/raw?ref=<ref>"
       glab api "projects/<project_id>/search?scope=blobs&search=<query>"
       glab api "projects/<project_id>/repository/commits?ref_name=<ref>&per_page=50"
   - To edit code, clone the project with `git clone <clone-url>` into the
     workspace and work there. Git is wired to glab's credential helper, so
     authenticated fetch/push work without extra setup.
2. Do the work for the detected task type.
3. Post the result back on GitLab (see "Reply target" below).
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
  question via the GitLab reply if you genuinely cannot proceed safely.
- Keep the GitLab reply concise and skimmable: short summary first, then details.
  Use Markdown, code fences, and `path:line` references.
- If you open a branch or merge request, link it in the reply.
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
   written and tested. Clone the project with `git clone <clone-url>` and work in
   that checkout.
3. Create a branch from the default branch named `<iid>-<short-slug>`.
4. Make the smallest coherent change. Update docs and the changelog if the
   project maintains them. Add or update tests.
5. Commit and push the branch. The glab credential helper supplies the token,
   so a plain `git push -u origin <branch>` works.
6. Open a merge request targeting the default branch, for example:
   `glab mr create -R <project> --source-branch <branch> --target-branch <default> --title "<title>" --description "<description>" --yes`
   In the description: summarise the change, list tests you ran, and reference
   the source (`Closes #<iid>` for issues). Mark it as a draft if it is incomplete.
7. Reply on the original thread with the merge request link and a short summary.

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


def build_prompt(cfg: Config, candidate: Candidate) -> str:
    task = classify(candidate.request)
    note_line = (
        str(candidate.note_id)
        if candidate.note_id is not None
        else "- (the trigger is the description)"
    )
    glab_target = "issue" if candidate.kind == "issue" else "mr"

    parts: list[str] = [INTRO]
    parts.append("## Trigger\n")
    parts.append(f"- Project: {candidate.project_path} (project_id: {candidate.project_id})\n")
    parts.append(f"- Clone URL: {clone_url(candidate.project_path)}\n")
    parts.append(f"- Target: {target_label(candidate)}\n")
    parts.append(f"- Trigger type: {trigger_label(candidate)}\n")
    parts.append(f"- Trigger note id: {note_line}\n")
    parts.append(f"- Requested by: @{candidate.author}\n")
    parts.append(f"- Thread: {candidate.web_url}\n")
    parts.append("\n### Raw trigger text\n```\n")
    parts.append(candidate.body.strip())
    parts.append("\n```\n")

    parts.append("\n### Requested action\n")
    if candidate.request.strip():
        parts.append("```\n")
        parts.append(candidate.request.strip())
        parts.append("\n```\n")
    else:
        parts.append("(The to-do body is empty — infer the task from the context above.)\n")
    parts.append(f"\nDetected task type: **{task}**. Follow the matching playbook below.\n")

    parts.append(HOW_TO_WORK)
    parts.append(RULES)
    parts.append(PLAYBOOKS)

    parts.append("\n## Reply target for this trigger\n")
    parts.append(
        f'- Post your reply with: `glab {glab_target} note {candidate.iid} '
        f'-R {candidate.project_path} -m "<markdown>"`\n'
    )
    if candidate.kind == "mr":
        parts.append(
            "- For inline feedback on specific lines, create a positioned discussion "
            "(needs base_sha, head_sha, start_sha, new_path, new_line):\n"
            "```\n"
            f"glab api projects/{candidate.project_id}/merge_requests/{candidate.iid}/discussions "
            "--method POST \\\n"
            "  -F 'body=<markdown>' \\\n"
            "  -F 'position={\"base_sha\":\"...\",\"head_sha\":\"...\",\"start_sha\":\"...\","
            "\"new_path\":\"path/to/file\",\"new_line\":12}'\n"
            "```\n"
        )
    parts.append(
        f'- Begin the note body with the marker `{cfg.reply_marker}` on its own line.\n'
    )
    parts.append(
        f"- Address the requester as @{candidate.author} and reference the repository "
        "paths and line numbers you used.\n"
    )

    if cfg.extra_prompt.strip():
        parts.append("\n## Additional instructions\n")
        parts.append(cfg.extra_prompt.strip())
        parts.append("\n")

    parts.append("\nBegin now.\n")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# opencode
# --------------------------------------------------------------------------- #

def run_opencode(cfg: Config, prompt: str) -> bool:
    os.makedirs(cfg.workdir, exist_ok=True)
    command = ["opencode", "run", "--standalone"]
    if cfg.auto:
        command.append("--auto")
    if cfg.model:
        command += ["--model", cfg.model]
    command.append(prompt)

    log(
        "running: opencode run --standalone"
        f"{' --auto' if cfg.auto else ''}"
        f"{' --model ' + cfg.model if cfg.model else ''}"
    )
    proc = subprocess.Popen(command, cwd=cfg.workdir)
    try:
        proc.wait(timeout=cfg.timeout)
    except subprocess.TimeoutExpired:
        log(f"opencode exceeded {cfg.timeout}s; killing it")
        proc.kill()
        proc.wait()
        return False
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# sweep
# --------------------------------------------------------------------------- #

def sweep(cfg: Config, me: dict, allowed_ids: Optional[set[int]]) -> int:
    candidates = discover_todos(cfg, me["id"], allowed_ids)
    candidates.sort(key=lambda c: c.updated_at)

    seen: set[str] = set()
    unique: list[Candidate] = []
    for candidate in candidates:
        if candidate.key() not in seen:
            seen.add(candidate.key())
            unique.append(candidate)
    log(f"found {len(unique)} to-do trigger(s)")

    handled = 0
    for candidate in unique:
        if handled >= cfg.max_triggers:
            log("reached --max; remaining to-dos wait for the next sweep")
            break

        awardable = candidate.awardable()
        if already_reacted(awardable, cfg.emoji, me["id"]):
            log(f"skip {candidate.key()} (already marked with '{cfg.emoji}')")
            mark_done(cfg, candidate)
            continue

        log(
            f"trigger: {target_label(candidate)} "
            f"[{trigger_label(candidate)}] by @{candidate.author}"
        )

        if cfg.dry_run:
            print("-" * 72)
            print(build_prompt(cfg, candidate))
            print("-" * 72)
            handled += 1
            continue

        try:
            react(awardable, cfg.emoji)
        except GlabError as error:
            log(f"could not add reaction to {candidate.key()}: {error}")
            continue

        if run_opencode(cfg, build_prompt(cfg, candidate)):
            log(f"handled {candidate.key()}")
        else:
            log(f"run failed for {candidate.key()}")
        mark_done(cfg, candidate)
        handled += 1

    return handled


def main(argv: list[str]) -> int:
    setup_environment()
    cfg = parse_args(argv)

    me = glab_api("user")
    if not isinstance(me, dict) or not me.get("id"):
        log(
            "cannot authenticate to GitLab: check "
            "GITLAB_PERSONAL_ACCESS_TOKEN/GITLAB_TOKEN and GITLAB_API_URL"
        )
        return 1

    allowed_ids = None
    if cfg.projects:
        allowed_ids = {project["id"] for project in resolve_projects(cfg)}

    log(f"gitlab-agent starting (as @{me.get('username')})")
    if cfg.dry_run:
        log("dry-run: no reactions will be added and OpenCode will not run")

    try:
        while True:
            try:
                handled = sweep(cfg, me, allowed_ids)
                log(f"sweep complete: {handled} to-do(s) handled")
            except GlabError as error:
                log(f"sweep failed: {error}")
            if cfg.once:
                return 0
            time.sleep(cfg.interval)
    except KeyboardInterrupt:
        log("interrupted; stopping")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
