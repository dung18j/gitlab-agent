# agent-runner

A small GitLab **todo worker**. It polls GitLab for a pending todo, *claims* it
with a comment, waits a few seconds to see whether another agent claimed first,
and only then runs [`opencode`](https://opencode.ai) to perform the request.

```
poll ─▶ claim (comment) ─▶ wait 5s ─▶ earliest claim wins ─▶ opencode run --auto ─▶ mark todo done
```

The image is based on **Debian trixie** and bundles:

- [`glab`](https://gitlab.com/gitlab-org/cli) — used for every GitLab API call
- [`opencode`](https://opencode.ai) V2 — the coding agent
- `python3`, `git`, `curl`, `tini`

The entrypoint is [`agent.py`](./agent.py).

## How it works

1. **Poll** `GET /todos?state=pending`.
2. **Identify the request.** Every todo maps to a request id: the triggering
   comment's note id (`note_<id>`, taken from `target_url` or matched by body and
   author) when it can be found, otherwise the todo id (`todo_<id>`).
3. **Claim** by posting a note tagged with that request id
   (`... <!-- CLAIM_MARKER request=<id> -->`), then react 👀 (`CLAIM_REACTION`) —
   on the comment when known, otherwise on the issue/MR.
4. **Wait** `CLAIM_WAIT_SECONDS` (default `5`) so competing agents can post their
   own claim.
5. **Race** — among the claim notes for the **same request id**, sort by
   `created_at` then `id` and take the earliest. If it is ours, we won; otherwise
   the earlier claimer handles it and we yield.
6. **Skip** — on later polls a request is skipped only when a claim note for its
   **own** request id exists, so a new comment on the same issue/MR is still
   processed even though an earlier comment was claimed.
7. **Run** `opencode run --auto` with a prompt built from the todo. For code
   changes it creates a branch, commits, and opens a merge request with `glab`
   (see [Cloning and making changes](#cloning-and-making-changes)).
8. **Finish** by (optionally) posting the output and marking the todo as done.

GitLab is the source of truth — the request-scoped claim notes and the 👀
reaction — so there is no local state file and the container is stateless. Only
the earliest claim for a request runs, so several agents can safely share a queue.

## Quick start

```bash
cp .env.example .env
$EDITOR .env          # set GITLAB_TOKEN and a provider key

docker build -t agent-runner .

docker run -d --name agent-runner --env-file .env \
  -v agent-workspace:/workspace \
  agent-runner

docker logs -f agent-runner
```

> The script loads `/app/.env` automatically when present, so you can either
> pass `--env-file .env` (recommended) or mount it:
> `-v "$PWD/.env:/app/.env:ro"`.

## Configuration

Everything is configured through environment variables (see
[`.env.example`](./.env.example)).

| Variable | Default | Description |
| --- | --- | --- |
| `GITLAB_TOKEN` | — | Personal access token with the `api` scope; add `read_repository` to clone (and `write_repository` to push). `GITLAB_PAT` is accepted as an alias. |
| `GITLAB_HOST` | `gitlab.com` | GitLab host, optionally with a scheme and port (e.g. `10.10.1.1:8080`). |
| `GITLAB_API_URL` | — | Full API URL alternative, e.g. `http://10.10.1.1:8080/api/v4`; host and scheme are derived from it. |
| `GLAB_API_PROTOCOL` | `https` | Protocol for API and clone URLs; derived from the host/URL above when possible. |
| `POLL_INTERVAL` | `30` | Seconds between polls when idle. |
| `CLAIM_WAIT_SECONDS` | `5` | Wait after claiming before checking for earlier claims. |
| `CLAIM_MARKER` | `opencode-agent-claim` | Hidden token in claim notes; claims are scoped per request id. |
| `CLAIM_REACTION` | `eyes` | Visible 👀 added on claim (on the comment when known). `none` disables. |
| `AGENT_NAME` | `opencode-agent` | Name shown in comments. |
| `CLAIM_MESSAGE` | generated | Override the claim comment body. |
| `OPENCODE_API_KEY` | — | Credential for the OpenCode Console provider. Other providers use their own variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY`, ...). |
| `OPENCODE_MODEL` | — | Model as `provider/model`, e.g. `anthropic/claude-sonnet-4`. |
| `OPENCODE_AGENT` | — | OpenCode agent to use (`--agent`). |
| `OPENCODE_ARGS` | — | Extra flags appended to `opencode run` (whitespace separated). |
| `OPENCODE_TIMEOUT` | `0` | Abort an `opencode run` after this many seconds (`0` = no limit). |
| `DEFAULT_ACTION` | `implement` | Action used when the request has no action word. |
| `AGENT_ACTIONS` | `implement explain review plan fix test refactor document analyze describe` | Action words recognised at the start of a request. |
| `TODO_ACTIONS` | *(all)* | Comma-separated `action_name` allow-list, e.g. `mentioned,assigned`. |
| `ALLOWED_PROJECTS` | *(all)* | Comma-separated project paths or ids to handle. Empty = every project. |
| `ALLOWED_REQUESTERS` | *(all)* | Comma-separated requester usernames or ids to handle. Empty = every requester. |
| `POST_RESULT` | `false` | Also post opencode's raw output as a comment (the agent posts its own reply). |
| `RESULT_MAX_CHARS` | `60000` | Truncate the raw output comment. |
| `REPLY_MARKER` | `🤖 <AGENT_NAME>` | Marker prefixed to every GitLab note the agent posts. |
| `EXTRA_PROMPT` | — | Extra instructions appended to the built-in prompt. |
| `WORKDIR` | `/workspace` | Directory opencode runs in. |
| `CLONE_REPO` | `false` | Clone the target project before running. |
| `CLONE_DIR` | `$WORKDIR/$project_path` | Override the clone directory. |
| `MR_BRANCH_PREFIX` | `agent/` | Branch name prefix; the request id is appended. |
| `MR_TARGET_BRANCH` | *(default branch)* | Target branch for the merge request. |
| `DRY_RUN` | `false` | Non-mutating: log the claim and prompt without calling GitLab or opencode. |

## Self-hosted GitLab

Point `GITLAB_HOST` at the instance. Include the scheme for plain HTTP (or use a
bare `host:port` plus `GLAB_API_PROTOCOL=http`):

```env
GITLAB_HOST=http://10.10.1.1:8080
```

At startup the agent sets `GITLAB_HOST` to a fully-qualified URL and exports
`API_PROTOCOL`/`GIT_PROTOCOL` (glab 1.53 reads those names; newer glab reads the
`GLAB_` ones), and pins them with `glab config set`. It never passes
`--hostname`, whose validator rejects `host:port`. Clones then use
`http://oauth2:<token>@10.10.1.1:8080/<project>.git`.

## The prompt

Every task gets **one prompt** built from the trigger context plus a set of
playbooks, so the same instructions cover implementation, code review,
explanation, testing, refactoring, and general questions:

- **Trigger** — project and project id, repository URL, target (issue/MR), the
  request id, requester, thread URL, and a suggested branch.
- **Raw trigger text** and the **requested action** (the todo body with a leading
  action word such as `implement` or `/review` stripped).
- **Detected task type** — classified from the request by `classify_request`
  (keyword based, matching the reference agent).
- **How to work / Rules / Progress updates / Playbooks** — ported from the
  reference agent; the playbooks tell opencode exactly what to do for the
  detected task type.
- **Reply target** — the exact `glab <issue|mr> note ...` command, plus the
  `REPLY_MARKER` to prefix each note with.

opencode posts its own progress updates and final reply with `glab`. Set
`POST_RESULT=true` if you also want the runner to post the raw output.

Bodies that reach opencode are untrusted user input. `opencode run --auto`
auto-approves permissions that are not explicitly denied, so review your
OpenCode [permission configuration](https://opencode.ai/v2/docs/permissions)
before running this against public projects.

## Cloning and making changes

The agent clones the project before running opencode, so it can make changes and
open a merge request:

```env
CLONE_REPO=true
```

The target project is cloned with `git` over **HTTPS using your PAT**
(`https://oauth2:<token>@<host>/<project>.git`) into `$WORKDIR/<group/project>`,
and opencode is run there. No SSH keys or `known_hosts` are needed; the token
just needs `read_repository` (add `write_repository` to push). Set
`GLAB_API_PROTOCOL=http` for plain-HTTP instances.

With `CLONE_REPO=true` the prompt tells opencode to publish its work: create a
branch named `<MR_BRANCH_PREFIX><request id>`, commit, and open a merge request
with the authenticated `glab` CLI:

```bash
glab mr create --fill --yes --source-branch agent/note_123 --repo group/project
```

`--fill` reuses the commit message and pushes the branch; the target branch
defaults to the project default unless `MR_TARGET_BRANCH` is set. Pushing needs
the token to have `write_repository`.

## Running several agents

To let multiple agents cooperate on one queue:

- give every agent the **same** `CLAIM_MARKER`; claims are matched by request id,
  so agents coordinate even when they use different tokens/accounts;
- make sure clocks are roughly in sync — the winner is decided by note
  `created_at` (then `id`);
- each agent needs its own GitLab token/account so it receives its own todos.

## Local development

The script needs only `python3`, `glab` and `opencode` (all present in the
image), with no third-party Python packages:

```bash
python3 -m py_compile agent.py         # syntax check
DRY_RUN=true GITLAB_TOKEN=... ./agent.py
```

The claim win/lose paths are easy to exercise by putting mock `glab` and
`opencode` executables earlier on `PATH`.
