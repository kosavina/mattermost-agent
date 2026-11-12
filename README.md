# mattermost-agent

A personal agent for your Mattermost account, in two processes. The **listener** watches for new posts, checks each one against your rules, groups matching posts into bursts, and queues each burst as a job. The **processor** takes each job, runs a locked-down AI coding agent (Codex or Claude Code) on it, and turns the answer into one of three outcomes: nothing, a new memory for that conversation, or a draft reply that waits for your approval on the terminal.

This is still a dry run towards Mattermost: nothing is ever posted. Approving a draft records the approval and stops there.

**What it does**

- Follows new posts live over a WebSocket, and reconnects by itself if the connection drops.
- After each reconnect, fetches the posts it missed in your direct messages and in the channels your rules name.
- Applies the first rule that matches a post. A rule matches on where the post is (a direct message, a channel) and who wrote it.
- Includes the posts you send. A rule applies to those only if its `from` list names you.
- Groups matching posts from one sender, in one place, written close together, into one **job**, and keeps them with their text for the processor.
- Logs one line per post and per change to a job, without the message text.
- Shows the queue on request: `mattermost-agent jobs`.
- Runs one agent per job, confined to the scenario's instructions, that conversation's own memory and the project folders your rule attaches, all read-only. The agent's answer is `ignore`, `remember` (a new memory, written by our code) or `act` (a draft).
- Shows waiting drafts with `drafts`, and takes `approve <id>` or `reject <id>`.

**What it does not do**

- It never posts, reacts or changes anything in Mattermost. It only reads. An approved draft is recorded, not sent.
- The agent holds no token and has no network: it can only read the folders it was given.
- It keeps the text of matched posts only, in its own database, for 7 days, and a conversation's memory until you forget it. Of every other post it keeps just the id.

## Quickstart

You need Docker with Compose, and a Mattermost server reachable over `https://`.

**1. Create a personal access token.** In Mattermost, open **Profile > Security > Personal Access Tokens** and create one. If that section is missing, ask your administrator to enable personal access tokens for your account. Copy the token, because it is shown only once.

**2. Get the two files you need**, in a new folder:

```sh
mkdir mattermost-agent && cd mattermost-agent
curl -fsSLO https://raw.githubusercontent.com/kosavina/mattermost-agent/main/compose.yaml
curl -fsSLO https://raw.githubusercontent.com/kosavina/mattermost-agent/main/rules.example.yaml
```

**3. Write your rules.** Copy the example and edit it. The comments at the top explain each field, and [Rules](#rules) below has the details.

```sh
cp rules.example.yaml rules.yaml
chmod 644 rules.yaml
```

**4. Save the token.** Paste it into the file that the editor opens, then save.

```sh
mkdir -m 700 secrets
nano secrets/mm_token
chmod 444 secrets/mm_token
```

The modes matter. The container runs as a different user, so it can read the token only if the file is readable by everyone (444). The folder (700) is what keeps other users of your computer out.

**5. Say which server and which version.** Create a file named `.env`. For `IMAGE_TAG`, use a full commit SHA from the [package page](https://github.com/kosavina/mattermost-agent/pkgs/container/mattermost-agent).

```sh
MM_URL=https://mattermost.example.com
IMAGE_TAG=<commit SHA>
```

The token never goes in `.env`.

**6. Start it.**

```sh
docker compose up -d
docker compose ps          # after about 30 seconds, the status should say "healthy"
docker compose logs -f     # watch the log, Ctrl+C to stop watching
```

If the configuration is wrong, the container stops with a one-line message that says what to fix.

**7. Look at the queue.**

```sh
docker compose exec agent mattermost-agent jobs      # one line per job, newest first
docker compose exec agent mattermost-agent jobs 1    # job 1 with its posts and their text
```

To stop it, run `docker compose down`. Your state stays in a Docker volume. To update, change `IMAGE_TAG` and run `docker compose up -d`. After editing `rules.yaml`, run `docker compose restart`.

## Rules

Rules are checked from top to bottom, and the first match wins. Every condition a rule sets must hold; within a list, any one item is enough. A rule needs at least one condition.

| Condition | Matches | Example |
| --- | --- | --- |
| `dm: true` | a direct message between two people. Group messages do not match. `dm: false` is refused; leave the key out instead | `dm: true` |
| `channels` | a post in one of these channels, by the name in the channel's URL | `channels: [analytics]` |
| `from` | a post by one of these usernames, without the `@`. Your own posts match only a rule that lists you here | `from: [example.user]` |

Each rule also has a `name`, which the log and the queue show, and a `scenario`, the processor's scenario for the job. A rule may add `read`, a list of project folder names under the agent root's `projects/` that the agent may read for that rule (one path segment each: letters, digits, `-` and `_`); without it, the agent sees only the scenario folder and the conversation's own memory.

A rule may also set `reply`, which says what happens to the agent's reply:

| `reply` | What happens | Who sends |
| --- | --- | --- |
| `local` | the draft waits on the terminal (`drafts`, `approve`, `reject`) | nobody |
| `draft` | the reply is put into that chat's message box as a Mattermost draft | you |
| `send` | the reply is posted at once with a label, if every check passes; otherwise it becomes a draft. The rule must list its senders in `from` | the listener |

A rule without `reply` uses `replies.default`, which is `draft` unless the file says otherwise. The modes are read and checked already, but delivery is not built yet: until it is, every reply waits on the terminal as with `local`.

The file ends with two more sections, both required:

```yaml
limits:
  jobs_per_hour: 20          # new jobs opened in any 60 minutes; posts joining a job don't count

bursts:
  quiet_seconds: 90          # a burst is ready after this long with no new post
  max_wait_seconds: 600      # and at the latest this long after its first post
  max_posts: 20              # and at the latest when it holds this many posts
```

The three burst settings are whole numbers of at least 1, and `quiet_seconds` may not be larger than `max_wait_seconds`.

An optional `replies` section sets the default reply mode and the limits for `send`. Every key is optional; these are the values used without it:

```yaml
replies:
  default: draft                 # local, draft or send, for rules without `reply`
  sends_per_hour: 5              # direct replies in any 60 minutes, in total
  sends_per_chat_per_hour: 2     # and in one chat
  max_send_age_minutes: 30       # a reply to an older burst becomes a draft
  label: "_Auto-reply by Kirill's assistant_"   # added to a direct reply on a line of its own
```

The numbers are whole numbers of at least 1, and the label is one line of at most 200 characters. Sending also needs `ALLOW_SEND=1` in the listener's environment; without it every `send` reply becomes a draft.

Being @mentioned is not a condition: an @mention in a channel no rule names does not open a job. Neither is the text of a post. The listener refuses a file it cannot fully understand (an unknown key, a condition of the wrong type, a missing section) with one line naming the rule number, so a typo cannot silently widen a rule.

## Bursts and jobs

People write a request as several short messages. A **burst** is the matching posts of one sender, in one place, written close together, and each burst becomes one **job**, the unit the processor will work on. One place means the same channel and the same thread: a reply in a thread and a top-level post in the same channel belong to different jobs.

- The first matching post opens a job, in status `collecting`. Each further post from the same sender in the same place joins it, as long as the same rule matched.
- The job becomes `ready` after `quiet_seconds` without a new post, or `max_wait_seconds` after its first post, or when it holds `max_posts` posts, whichever comes first. A post that arrives later opens a new job.
- Opening a job counts against `jobs_per_hour`. Over the limit, the post is logged as `cap_hit` and no job is opened. Posts joining a job never count.
- Timing uses the listener's clock when it handles the post, so posts fetched during catch-up are grouped as well.
- A job and its posts are deleted 7 days after the job opened, whatever its status. The text is overwritten in the database file at that point.

The processor will move a job on through `running`, `waiting_approval`, `done` and `failed`; the listener never touches a job after `ready`.

**Where the state lives.** The database and the heartbeat are in the Docker volume `agent_data`. If you set `STATE_DIR` yourself, point it at a local disk, never at a network share or a VM shared folder (such as `/Volumes/My Shared Files` on a Mac): the listener and the `jobs` command share one SQLite file in WAL mode, which needs a local file system.

## Processor

The processor is a second process next to the listener. In this version both run natively on a Mac (`uv run`), sharing the state folder; the Docker image is the listener only, and the processor's packages are not in it. Only one processor may run per state folder: it holds `state/processor.lock` for its whole run, and a second `process` command exits at once.

**Folders.** Everything lives under one agent root, `~/agent`, on the local disk (never under `/Volumes`, where neither SQLite nor Codex's sandbox work):

```text
~/agent/                 mode 700
  state/                 STATE_DIR: the listener's state.sqlite3, the processor's checkpoints.sqlite3 and processor.lock
  scenarios/             your scenario folders (trusted instructions) and scenarios.yaml
    scenarios.yaml
    intake/AGENTS.md
  projects/              read-only materials a rule can attach: projects/<name>
  conversations/         written only by the processor: conversations/<channel_id>/memory.md and history/
  codex-home/            the agent's own CODEX_HOME with its auth.json (mode 700)
```

`scenarios.yaml` maps each scenario a rule names to a folder, and optionally to a backend:

```yaml
scenarios:
  intake:
    folder: intake        # ~/agent/scenarios/intake, which must hold AGENTS.md
    backend: codex        # optional: codex or claude; the default is AGENT_BACKEND
```

A scenario folder must hold `AGENTS.md`, the instructions the agent gets, and must not hold `CLAUDE.md`, `CLAUDE.local.md` or `.claude/`. Each direct message, group message and channel gets its own folder under `conversations/`, named by its channel id, so an agent never sees another conversation's memory. The agent only proposes a memory; the processor writes it, keeps every earlier version in `history/`, and never creates any other file there.

**Settings** (environment variables, checked once at startup; errors name the variable, never its value):

| Variable | Meaning | Default |
| --- | --- | --- |
| `STATE_DIR` | The listener's state folder | required |
| `AGENT_ROOT` | The agent root above | required |
| `RULES_FILE` | The listener's `rules.yaml` | required |
| `AGENT_BACKEND` | `codex` or `claude`, for scenarios that name none | `codex` |
| `CODEX_BIN`, `CODEX_HOME`, `CODEX_INSTALL_DIR` | Where Codex is | `/opt/homebrew/bin/codex`, `~/agent/codex-home`, `/opt/homebrew/Caskroom/codex` |
| `CLAUDE_BIN` | Where Claude Code is | `/opt/homebrew/bin/claude` |
| `AGENT_TIMEOUT_SECONDS` | One agent run | `120` |
| `POLL_SECONDS` | How often the queue is polled | `3` |
| `LOG_LEVEL` | As for the listener | `INFO` |

The processor refuses to start if any `MM_*` variable is set (it must never hold the token), if a folder is on `/Volumes`, if a folder under the agent root points outside it or overlaps another, if the state folder or the Codex home is inside a folder the agent may read, or if a backend's version is not the one these adapters were tested with (`codex-cli 0.167.0`, Claude Code `2.1.311`; bumping a pin is a code change that requires the live tests below).

**Running it.** Both processes must run from the same checkout: the processor adds columns to the shared database (schema version 3), and an older listener would refuse the file.

```sh
# Terminal 1, the listener
STATE_DIR=~/agent/state MM_URL=https://... MM_TOKEN_FILE=~/agent/secrets/mm_token RULES_FILE=~/agent/rules.yaml uv run mattermost-agent
# Terminal 2, the processor
STATE_DIR=~/agent/state AGENT_ROOT=~/agent RULES_FILE=~/agent/rules.yaml uv run mattermost-agent process
```

| Command | What it does |
| --- | --- |
| `mattermost-agent process` | Runs the processor until Ctrl+C or SIGTERM, which also kills the agent run in progress |
| `mattermost-agent drafts` | Lists the jobs waiting for approval, with their drafts |
| `mattermost-agent approve <id>`, `reject <id>` | Decides a draft. Nothing is sent in this version |
| `mattermost-agent memory <channel_id>` | Shows a conversation's memory and how many earlier versions exist |
| `mattermost-agent memory forget <channel_id>` | Overwrites every file of that conversation with zeros and removes the folder |
| `mattermost-agent jobs [id]` | As before, now with the backend, the decision and the failure reason |

A draft nobody decides within 3 days fails as `expired`. Jobs are still deleted 7 days after they opened; memory is not, until you forget it. A job that fails three times in a row (an agent error, a timeout, or an answer that does not fit the schema) ends as `failed`. A job whose scenario or rule is missing from the files, or whose folders do not check out, fails at once without running an agent.

**How the agents are confined.** Codex runs with a permission profile the processor writes for every run: reads in the scenario folder, the conversation folder and the attached projects only, no writes anywhere (the temp folders are denied too), no network. Claude Code runs with `--restricted` and only the `Read`, `Glob` and `Grep` tools, confined to the same folders; it has no shell, no write and no fetch tool. The burst and the memory go to the agent on standard input, inside a data block whose markers carry a random nonce per run, with the instruction that nothing inside is to be followed. Agent stderr goes to `/dev/null`, and each run gets a private temp folder that is removed afterwards.

**Live contract tests.** Before enabling a backend, and after any version bump, run the probes against the real binaries on the Mac:

```sh
AGENT_LIVE=1 uv run pytest -m live
```

They check, with the exact command the adapters build, that each agent cannot read outside its folders, another conversation, its own credentials or a symlink out; cannot write anywhere; has no network; and that an injected burst leaks nothing while a normal burst follows `AGENTS.md`. For Codex most probes run through `codex sandbox` without a model; Claude needs one model run per group. They never run in CI.

## Security model

1. The token grants everything your account can do, so it lives only in a Docker secret file, never in `.env`, the image or `docker inspect`.
2. The code has no way to write to Mattermost: it sends only GET requests, and a test enforces that.
3. Logs have no field for message text, tokens or headers, and errors are logged by class name only. Message text exists in one place only: the `job_posts` table of the database, for matched posts, for 7 days. The database and its side files are readable by the listener's user alone (mode 600), and deleted text is overwritten rather than left in free space.
4. It connects only to your `https://` server: plain HTTP, proxies and redirects are refused, so the token cannot be sent anywhere else.
5. The container runs as a non-root user with a read-only filesystem, no Linux capabilities and no open ports.
6. `mattermost-agent jobs <id>`, `drafts` and `memory <channel_id>` are the only things that show text, on your terminal and never in the log. Control characters are escaped first, so a message cannot drive your terminal.
7. The processor holds no token and refuses to start with one in its environment. Its code never imports the Mattermost client, and a test checks that.
8. Our code decides every flag, folder and permission of an agent run; nothing in a message, in a memory or in the agent's answer can change them. The agent reads only the scenario folder, the conversation's own folder and the attached projects, all resolved through symlinks and checked against their roots, and cannot write or reach the network. Its answer is parsed into three fields and used as data.
9. The agent proposes a memory; the processor writes it. A conversation's memory is built from other people's messages, so it is untrusted data: it goes to the agent inside the data block, it is kept in a folder only the processor writes, and `memory forget` overwrites it with zeros before deleting it.
10. The processor's logs carry only numbers and fixed words about a run: the backend, the decision, the duration, the attempt, how many tool calls were refused. The prompt, the memory, the draft and the agent's output never reach a log line or an error message. The checkpoint file holds a running job's memory and draft until the job ends, never a post, with mode 600 and overwritten on deletion.

## Reading the logs

Every line starts with the time, the level and `mattermost_agent`. Values are padded with spaces so that columns line up; the examples here leave that out. A very long value pushes the rest of its own line to the right. A post looks like this:

```text
event=post kind=dm channel=null sender="example.user" length=42 rule="dm-from-anyone" scenario="intake"
```

| Field | Meaning |
| --- | --- |
| `event` | `post`, or `cap_hit` if the post matched a rule but the hourly limit was already reached |
| `kind` | `dm`, `group_dm`, `mention`, `thread_reply` or `channel`. Rules do not use it; it is kept with the post for the processor |
| `channel` | The channel's URL name. For a direct message you sent, `@` and the recipient's username. Otherwise `null` for direct and group messages |
| `sender` | The sender's username, or `null` if it could not be looked up |
| `length` | Number of characters in the post. This is the only thing logged about its text |
| `rule`, `scenario` | The rule that matched and its label, or `null` if none did |

A matched post is followed by a line about its job:

```text
event=job_joined job=3 status=collecting scenario="intake" rule="dm-from-anyone" place="@example.user" posts=2
```

| Event | Meaning |
| --- | --- |
| `job_opened` | The post opened job number `job` |
| `job_joined` | The post joined the job, which now holds `posts` posts |
| `job_ready` | The job is complete and waits for the processor |

`place` is the channel's name, or `@` and the other person's username for a direct message, or `null` if that is unknown.

Duplicates, deleted posts and system messages ("X joined the channel") are skipped and not logged.

Lines about the listener itself have this shape:

```text
event=disconnected reason=unreachable error=ConnectError status=null count=null delay_s=null
```

| Event | Meaning |
| --- | --- |
| `startup`, `connected` | It logged in, then opened the live connection |
| `disconnected`, `reconnect_wait` | The connection dropped. It retries after `delay_s` seconds, at most 60 |
| `catchup_start`, `catchup_done` | Fetching missed posts. `count` is channels at the start and posts at the end |
| `catchup_truncated` | It could not fetch everything, because the outage passed 30 days, there were too many posts, or a channel was skipped. Some posts were left out |
| `channel_skipped` | The server refused one channel during catch-up, or sent something it could not read. `status` says which. It carries on with the others |
| `lookup_failed` | It could not find out who a user is, so the post has no sender. It carries on |
| `frame_skipped` | The server sent something it could not read. It carries on |
| `jobs_pruned` | The hourly clean-up deleted `count` jobs older than 7 days, with their text |
| `drafts_expired`, `threads_pruned` | Processor only: `count` drafts nobody decided in 3 days failed as `expired`; `count` leftover checkpoint threads were deleted |
| `shutdown`, `stopped` | A normal stop |
| `internal_error` | A bug in a background task. Please report it |
| `fatal` | It gave up. `reason` says why |

`reason` is one of `auth_rejected` (the token is wrong or expired), `unreachable`, `server_error`, `protocol_error`, `store_error` or `signal`.

The processor adds four events:

```text
event=agent_run  job=12 backend=codex scenario="intake" decision=act duration_s=10.8 attempt=1 denials=0
event=agent_error job=12 backend=claude reason=timeout exit_code=null attempt=2
event=memory_saved job=12 channel="<channel_id>" chars=812 version=4
event=job_done   job=12 status=waiting_approval fail_reason=null
```

| Event | Meaning |
| --- | --- |
| `agent_run` | One agent run finished with `decision`. `denials` is how many tool calls the agent's own controls refused (Claude reports them; Codex cannot) |
| `agent_error` | A run failed: `exit` (non-zero exit code), `timeout`, `invalid_json` or `invalid_output` (an answer that does not fit the schema), `denied_startup`. It is tried again up to three times |
| `memory_saved` | The conversation's memory was replaced; `chars` is its length and `version` its number |
| `job_done` | The job left the processor: `status` is `waiting_approval`, `done` or `failed`, and `fail_reason` one of `agent_failed`, `agent_timeout`, `invalid_output`, `unknown_scenario`, `unknown_rule`, `bad_folder`, `expired` |

`LOG_LEVEL` in `.env` sets the level: `DEBUG`, `INFO`, `WARNING` or `ERROR`. The default is `INFO`.

## Known limits

- **Missed posts.** Catch-up covers direct messages and channels named in your rules. A post that a `from` rule would match in any other channel, while the listener is disconnected, is not seen.
- **Edited and deleted posts.** A job keeps a post's text as it was when the post arrived. A later edit is not seen, and a post deleted later stays in its job.
- **Posts without a sender.** After `lookup_failed` the post is logged with `sender=null`, and a rule with `from` cannot match it. It is not tried again.
- **Mentions.** Being @mentioned is recorded as `kind=mention` but is not a condition. Catch-up recognises only a literal `@yourname` for that.
- **First run.** It starts from now and does not read old posts.
- **Long outages.** Catch-up goes back at most 30 days.
- **Rules** are read at startup, so a change needs a restart.
- **Approval on the terminal only.** Drafts are shown and decided with `drafts`, `approve` and `reject`; nothing is sent to Mattermost yet, and the listener does not yet know the agent's own posts.
- **Claude Code uses your own login.** Until it gets a separate config folder and token, every run reads your Keychain login and rewrites `~/.claude.json` (with a backup under `~/.claude/backups/`). It leaves no transcript and no memory. Codex uses the separate `~/agent/codex-home`.
- **A `kill -9` during a run** leaves the agent to finish or time out on its own; the next start removes its temp folder and runs the job once more.
- **Mac only, natively.** The agents' confinement is verified on macOS. A container for the processor, and a container wall around both, are the next milestone.
- **Health** means the process is not stuck. During a Mattermost outage the container stays healthy and the log shows the reconnects. A rejected token ends the process, and Compose then starts it again, so look for `reason=auth_rejected` if it keeps restarting.
