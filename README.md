# mattermost-agent

A listener for your Mattermost account. It watches for new posts, sorts each one, checks it against your rules, and writes one log line.

This is a dry run, the first milestone of a personal agent.

**What it does**

- Follows new posts live over a WebSocket, and reconnects by itself if the connection drops.
- After each reconnect, fetches the posts it missed in your direct messages and in the channels your rules name.
- Sorts each post as `dm`, `group_dm`, `mention`, `thread_reply` or `channel`, then applies the first rule that matches.
- Logs one line per post, without the message text.

**What it does not do**

- It never posts, reacts or changes anything in Mattermost. It only reads.
- It calls no AI service.
- It stores no message text. Its database holds post ids and timestamps only.

## Quickstart

You need Docker with Compose, and a Mattermost server reachable over `https://`.

**1. Create a personal access token.** In Mattermost, open **Profile > Security > Personal Access Tokens** and create one. If that section is missing, ask your administrator to enable personal access tokens for your account. Copy the token, because it is shown only once.

**2. Get the two files you need**, in a new folder:

```sh
mkdir mattermost-agent && cd mattermost-agent
curl -fsSLO https://raw.githubusercontent.com/kosavina/mattermost-agent/main/compose.yaml
curl -fsSLO https://raw.githubusercontent.com/kosavina/mattermost-agent/main/rules.example.yaml
```

**3. Write your rules.** Copy the example and edit it. The comments at the top explain each field.

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

To stop it, run `docker compose down`. Your state stays in a Docker volume. To update, change `IMAGE_TAG` and run `docker compose up -d`. After editing `rules.yaml`, run `docker compose restart`.

## Security model

1. The token grants everything your account can do, so it lives only in a Docker secret file, never in `.env`, the image or `docker inspect`.
2. The code has no way to write to Mattermost: it sends only GET requests, and a test enforces that.
3. Logs have no field for message text, tokens or headers, and errors are logged by class name only.
4. It connects only to your `https://` server: plain HTTP, proxies and redirects are refused, so the token cannot be sent anywhere else.
5. The container runs as a non-root user with a read-only filesystem, no Linux capabilities and no open ports.

## Reading the logs

Every line starts with the time, the level and `mattermost_agent`. A post looks like this:

```text
event=post kind=mention channel="analytics" sender="example.user" length=42 rule="mentions-me" scenario="intake"
```

| Field | Meaning |
| --- | --- |
| `event` | `post`, or `cap_hit` if the post matched a rule but the hourly limit was already reached |
| `kind` | `dm`, `group_dm`, `mention`, `thread_reply` or `channel` |
| `channel` | The channel's URL name, or `null` for direct and group messages |
| `sender` | The sender's username, or `null` if it could not be looked up |
| `length` | Number of characters in the post. This is the only thing logged about its text |
| `rule`, `scenario` | The rule that matched and its label, or `null` if none did |

Your own posts, duplicates, deleted posts and system messages ("X joined the channel") are skipped and not logged.

Lines about the listener itself have this shape:

```text
event=disconnected reason=unreachable error=ConnectError status=null count=null delay_s=null
```

| Event | Meaning |
| --- | --- |
| `startup`, `connected` | It logged in, then opened the live connection |
| `disconnected`, `reconnect_wait` | The connection dropped. It retries after `delay_s` seconds, at most 60 |
| `catchup_start`, `catchup_done` | Fetching missed posts. `count` is channels at the start and posts at the end |
| `catchup_truncated` | It could not fetch everything, because the outage passed 30 days or there were too many posts. Some older posts were left out |
| `frame_skipped` | The server sent something it could not read. It carries on |
| `shutdown`, `stopped` | A normal stop |
| `internal_error` | A bug in a background task. Please report it |
| `fatal` | It gave up. `reason` says why |

`reason` is one of `auth_rejected` (the token is wrong or expired), `unreachable`, `server_error`, `protocol_error`, `store_error` or `signal`.

`LOG_LEVEL` in `.env` sets the level: `DEBUG`, `INFO`, `WARNING` or `ERROR`. The default is `INFO`.

## Known limits

- **Missed mentions.** Catch-up covers direct messages and channels named in your rules. A mention in any other channel while the listener is disconnected is not seen.
- **Mentions found by catch-up.** Catch-up recognises only a literal `@yourname`. A post that reaches you through `@all`, `@here` or a mention keyword counts as a mention only when it arrives live.
- **First run.** It starts from now and does not read old posts.
- **Long outages.** Catch-up goes back at most 30 days.
- **Rules** are read at startup, so a change needs a restart.
- **Health** means the process is not stuck. During a Mattermost outage the container stays healthy and the log shows the reconnects. A rejected token ends the process, and Compose then starts it again, so look for `reason=auth_rejected` if it keeps restarting.
