# gitlab-agent

A containerised [OpenCode](https://opencode.ai) v2 agent that works your GitLab
to-do list. It talks to GitLab through the
[`glab`](https://gitlab.com/gitlab-org/cli) GitLab CLI and runs inside the
container — you only run the container with a `.env` file.

- **Base image:** Debian 13 "trixie" (slim)
- **OpenCode:** v2 CLI (`@opencode/cli`)
- **GitLab CLI:** `glab` (pinned release, multi-arch)
- **Bot:** `agent.py` (Python 3, standard library only)
- **Configuration:** a `.env` file, read at container start — no secrets in the image

## Configuration

```sh
cp .env.example .env
$EDITOR .env
```

At minimum set the GitLab personal access token and one model-provider key:

```dotenv
GITLAB_PERSONAL_ACCESS_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
GITLAB_API_URL=https://gitlab.com/api/v4
ANTHROPIC_API_KEY=...
```

The token needs the `api` scope (to react and reply) and the
`write_repository` scope (to clone and push over HTTPS). Inside the container it
is exposed to `glab` as `GITLAB_TOKEN`, and the instance URL as `GITLAB_HOST`;
`glab` authenticates from those, so no `glab auth login` step is needed.

`.env` is git-ignored and never written into the image. Pass it at runtime with
`podman run --env-file .env`.

## Build

```sh
podman build -t localhost/gitlab-agent:latest .
```

## Run

Start the bot (this is the image's default command). It loops forever, polling
GitLab every 60s:

```sh
podman run --rm --env-file .env \
  -v "$PWD/workspace:/workspace:Z" \
  localhost/gitlab-agent:latest
```

The mount is the agent's working directory; it can be omitted to use an
ephemeral `/workspace`. The default command is the bot; to pass flags, name it
again (`gitlab-agent <flags>`):

```sh
# one sweep and exit
podman run --rm --env-file .env localhost/gitlab-agent:latest gitlab-agent --once

# print the prompts without reacting, running OpenCode, or marking to-dos done
podman run --rm --env-file .env localhost/gitlab-agent:latest gitlab-agent --dry-run --once
```

To use OpenCode interactively instead of the bot, override the command:

```sh
podman run --rm -it --env-file .env \
  -v "$PWD/workspace:/workspace:Z" \
  localhost/gitlab-agent:latest opencode
```

## How it works

1. **Discover** — lists the authenticated user's pending GitLab to-dos and keeps
   the `mentioned` and `directly_addressed` actions, which carry the request text
   in the comment body.
2. **Claim** — reacts with 👀 on the matching comment (or issue/MR). Anything
   that already carries the reaction from the bot account is skipped, so re-runs
   are idempotent. To-dos whose body is the agent's own reply are always ignored,
   so it never triggers itself.
3. **Act** — renders a task-aware prompt and runs `opencode run --auto` directly
   in the container. The prompt tells OpenCode to read the full context through
   `glab` and then perform the task.
4. **Reply** — OpenCode posts its result back to the GitLab thread with the
   `🤖` marker.
5. **Done** — the to-do is marked as done (`glab todo done`), unless
   `--no-mark-todo` is set.

## Trigger

A to-do is created when someone **mentions** the bot account or **directly
addresses** it in a comment, e.g. `@gitlab-agent implement retries for the
uploader`. Any such mention is a request; there is no keyword to remember.

The prompt classifies the request and includes a playbook for each kind of work:
**review**, **implement**, **explain**, **test**, **refactor**, and a general
fallback. For example a mention asking to review an MR triggers the code-review
playbook, which fetches the MR diff and reports prioritised findings as an MR
note.

## Options

Every option is also read from the matching environment variable (handy with
`--env-file`).

| Flag | Env | Default | Purpose |
| --- | --- | --- | --- |
| `--project <id\|path,…>` | `PROJECTS` | all projects | Only handle to-dos from these projects |
| `--author <username,…>` | `AUTHORS` | any author | Only handle to-dos authored by these GitLab users |
| `--model <provider/model>` | `OPENCODE_MODEL` | OpenCode default | Passed to `opencode run --model` |
| `--workdir <dir>` | `WORKSPACE` | `/workspace` | Directory OpenCode runs in |
| `--interval <s>` | `INTERVAL` | `60` | Seconds between sweeps |
| `--timeout <s>` | `TIMEOUT` | `3600` | Kill an OpenCode run after this long |
| `--max <n>` | `MAX` | `1` | To-dos handled per sweep |
| `--ignore-self` | `IGNORE_SELF` | `true` | Ignore to-dos authored by the bot account (its replies are always ignored) |
| `--mark-todo` | `MARK_TODO` | `true` | Mark the to-do done after handling it |
| `--emoji <name>` | `EMOJI` | `eyes` | Reaction used as the handled marker |
| `--reply-marker <s>` | `REPLY_MARKER` | `🤖` | First line of the agent's reply |
| `--extra-prompt <s>` | `EXTRA_PROMPT` | none | Extra instructions appended to every prompt (use `\n` for line breaks) |
| `--once` | — | off | Run a single sweep and exit |
| `--dry-run` | — | off | Print prompts, react/run nothing |
| `--no-auto` | — | off | Do not pass `--auto` to `opencode run` |

## Requirements

- The GitLab token needs the `api` scope (to react or reply) and the
  `write_repository` scope (to clone and push over HTTPS).
- To-dos are per user, so the token's account receives them when it is mentioned
  or directly addressed. Use `--project` to ignore to-dos from other projects,
  and `--author` to only act on mentions from specific people.

## Limitations

- Only the `mentioned` and `directly_addressed` to-do actions are handled; other
  actions (assigned, review requested, …) have no request body and are ignored.
- Discovery uses `per_page=100` rather than `glab api --paginate`, because some
  self-hosted instances return page links that omit a non-standard port.

## Layout

| File | Purpose |
| --- | --- |
| `Dockerfile` | Image definition; installs OpenCode, `glab`, and the bot |
| `agent.py` | The bot (Python 3, standard library only) |
| `opencode.json` | Global OpenCode config |
| `.env.example` | Template for `.env` |
| `README.md` | This file |
