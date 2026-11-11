# mattermost-agent

A listener for your Mattermost account. It watches for new posts, checks each one against your rules, groups matching posts into bursts, and queues each burst as a job.

This is the reading half of a personal agent, running as a dry run: it reads and queues, and the processor that will act on the jobs does not exist yet.

**What it does**

- Follows new posts live over a WebSocket, and reconnects by itself if the connection drops.
- After each reconnect, fetches the posts it missed in your direct messages and in the channels your rules name.
- Applies the first rule that matches a post. A rule matches on where the post is (a direct message, a channel) and who wrote it.
- Includes the posts you send. A rule applies to those only if its `from` list names you.
- Groups matching posts from one sender, in one place, written close together, into one **job**, and keeps them with their text for the processor.
- Logs one line per post and per change to a job, without the message text.
- Shows the queue on request: `mattermost-agent jobs`.

**What it does not do**

- It never posts, reacts or changes anything in Mattermost. It only reads.
- It calls no AI service.
- It keeps the text of matched posts only, in its own database, for 7 days. Of every other post it keeps just the id.

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

## Security model

1. The token grants everything your account can do, so it lives only in a Docker secret file, never in `.env`, the image or `docker inspect`.
2. The code has no way to write to Mattermost: it sends only GET requests, and a test enforces that.
3. Logs have no field for message text, tokens or headers, and errors are logged by class name only. Message text exists in one place only: the `job_posts` table of the database, for matched posts, for 7 days. The database and its side files are readable by the listener's user alone (mode 600), and deleted text is overwritten rather than left in free space.
4. It connects only to your `https://` server: plain HTTP, proxies and redirects are refused, so the token cannot be sent anywhere else.
5. The container runs as a non-root user with a read-only filesystem, no Linux capabilities and no open ports.
6. `mattermost-agent jobs <id>` is the only thing that shows message text, on your terminal and never in the log. Control characters in a message are escaped first, so a message cannot drive your terminal.

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
| `shutdown`, `stopped` | A normal stop |
| `internal_error` | A bug in a background task. Please report it |
| `fatal` | It gave up. `reason` says why |

`reason` is one of `auth_rejected` (the token is wrong or expired), `unreachable`, `server_error`, `protocol_error`, `store_error` or `signal`.

`LOG_LEVEL` in `.env` sets the level: `DEBUG`, `INFO`, `WARNING` or `ERROR`. The default is `INFO`.

## Known limits

- **Missed posts.** Catch-up covers direct messages and channels named in your rules. A post that a `from` rule would match in any other channel, while the listener is disconnected, is not seen.
- **Edited and deleted posts.** A job keeps a post's text as it was when the post arrived. A later edit is not seen, and a post deleted later stays in its job.
- **Posts without a sender.** After `lookup_failed` the post is logged with `sender=null`, and a rule with `from` cannot match it. It is not tried again.
- **Mentions.** Being @mentioned is recorded as `kind=mention` but is not a condition. Catch-up recognises only a literal `@yourname` for that.
- **First run.** It starts from now and does not read old posts.
- **Long outages.** Catch-up goes back at most 30 days.
- **Rules** are read at startup, so a change needs a restart.
- **Health** means the process is not stuck. During a Mattermost outage the container stays healthy and the log shows the reconnects. A rejected token ends the process, and Compose then starts it again, so look for `reason=auth_rejected` if it keeps restarting.
