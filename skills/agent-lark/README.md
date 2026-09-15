# agent-lark

English · [中文](README.zh-CN.md)

Let any AI coding CLI push the decisions it cannot make on its own to your phone through
[Feishu / Lark](https://www.feishu.cn), and send your verdict — a tap, a tick, or a typed sentence — straight back
into the agent's session that asked. Messages, photos and voice notes you send on your own land in that session
too. No server, no public URL: the daemon dials out to Feishu; the app is created by scanning a QR code with your
phone, no workspace-admin approval needed.

This file is for the person installing it. The agent reads [SKILL.md](SKILL.md) and `references/`;
you never need to explain the tool to it. It is the Feishu sibling of `agent-ntfy` in the same repository; how the
two compare is in the [repository README](../../README.md).

## Contents

1. How it works
2. Requirements (2.1 platform support · 2.2 herdr is optional)
3. Install
4. Setup (4.1 scan a QR code · 4.2 use an app you already have · 4.3 scopes · 4.4 where credentials are read from)
5. First question, end to end
6. Where things live
7. Day-to-day
8. If something goes wrong
9. Security
10. Known limits
11. Environment variables
12. CLI reference
13. Versions and upgrading
14. Integration: keeping the skill in force for the whole session

## 1. How it works

```
agent ──ask (JSON on stdin)──▶ agent-lark ──local socket──▶ daemon ──outbound to Feishu──▶ your phone
      ◀── reply on stdout ────            ◀──────────────         ◀── events ──────────      ◀── tap / tick / type
                                                                      │
                                                          no question pending?
                                                                      ▼
                                                    injected into the agent's herdr pane
```

- The agent hands over a JSON with eight required fields; the CLI renders it into a Feishu card — one button per option, or a form of checkboxes with a Submit button — and pushes it into the project's group. The agent blocks until you tap, tick or type; your reply is returned verbatim. With `notify` it can push a one-way card (title + body, no button) and carry on; with `send-file` it can send a screenshot or a file.
- A resident **daemon** holds the single connection to Feishu. Anything you send in the group while no question is pending is injected into the agent's session as an instruction prefixed with `[agent-lark remote] `; once it has landed, your message gets a `Get` reaction (this is the one part that needs herdr; §2.2 lists what works without it).
- **One project, one group.** A project is the git toplevel of the directory the agent works in (otherwise that directory); `away on` creates a Feishu group for it — or offers back a group it used earlier — and everything the project sends goes there. Whichever group you speak in is the project you are speaking to, so an instruction never reaches the wrong agent.
- The channel carries text and files; it never interprets them, never answers for you.
- The local socket is a Unix socket on macOS / Linux and a named pipe on Windows (§6).

## 2. Requirements

- Node.js 22 or newer. The CLI ships as one self-contained file (`dist/cli.mjs`, the Feishu SDK bundled in): nothing to `npm install`, nothing to build
- A Feishu / Lark account — a personal one is enough. The custom app is created from your phone by scanning a QR code (§4.1); an app someone else created works too (§4.2)
- Outbound access to Feishu's servers from the machine the daemon runs on
- A POSIX shell for the examples in this file and in SKILL.md (heredocs, `alias`). On Windows that means Git Bash or WSL; the daemon and the CLI themselves run natively
- [herdr](https://herdr.dev), optional — see §2.2

### 2.1 Platform support

| platform | status |
|---|---|
| macOS | The whole chain is tested on real machines: `setup` by QR code, daemon, `away on` creating, renaming, letting go of and taking back groups, `ask` (buttons, checkboxes, a typed reply to a pending question, the urgent flag), `notify`, `send-file`, text / photos / voice notes from the phone with herdr injection, the stuck-on-a-prompt card, `daemon --stop` with and without `--force`. Credentials in the keychain |
| Linux | **Unit tests on CI only** (ubuntu-latest, Node 22 and 24). No end-to-end run in a real environment yet — pull requests welcome. Credentials in libsecret (`secret-tool`) when it is installed, else a `0600` file |
| Windows 10 / 11 | **Unit tests on CI only** (windows-latest, Node 22 and 24, including the named-pipe transport). No end-to-end run in a real environment yet — pull requests welcome. Runs natively; use Git Bash or WSL for the shell examples. Credentials in a DPAPI-encrypted file. Phone → agent injection through herdr is untested there |

### 2.2 herdr is optional: what you keep and what you lose without it

[herdr](https://herdr.dev) is the terminal multiplexer this skill uses to deliver text *into* an agent's session (`brew install herdr` on macOS / Linux; see https://herdr.dev for other platforms). It is the only optional piece, and this is exactly what depends on it:

| works without herdr | needs herdr |
|---|---|
| The whole `ask` round trip: card on the phone → tap / tick / type → reply on stdout → exit code; `notify`; `send-file` | Phone → agent messages when no question is waiting: an instruction you send on your own initiative, a photo, a voice note, or a reply to a card that has already been answered, timed out or cancelled |
| The daemon and every other subcommand: `setup`, `away`, `rename`, `unbind`, `bind`, `status` | The 🔔 *waiting for you* card: pushed while remote mode is on and the agent is stuck on a prompt only a human can answer (a permission dialog, a multiple-choice question) |
| Groups and bindings are per project either way; without herdr no pane is recorded, so there is nothing to inject into | |

Such a message is never dropped silently. The daemon answers in the group with an orange receipt card titled `[<project dir>] Not delivered` and a body saying why (no pane recorded for this project; the recorded pane is gone; herdr is not running; the agent is stuck on a prompt and cannot take input); and `away on` run outside herdr ends with `Not inside herdr: messages sent from the phone are not injected anywhere, and there is no stuck-on-a-prompt alert.`

Why herdr and nothing else: injecting means writing a line of text into the target agent's terminal (its PTY), and `herdr agent prompt` is the one generic way to do that for any agent CLI; this skill has no fallback mechanism.

## 3. Install

Into the current project (by default the skill lands in `./.agents/skills/agent-lark`, with a symlink from `./.claude/skills/agent-lark`; with a single non-universal agent selected via `-a <agent>` the CLI copies it into that agent's directory instead):

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-lark
```

For all projects at once, add `-g`: the files go to `~/.agents/skills/agent-lark` and `~/.claude/skills/agent-lark` becomes a symlink to them.

> **Warning about `-g`.** If `~/.claude/skills/agent-lark` already exists as a real directory (a copy you put there by hand), the `skills` CLI deletes it and replaces it with the symlink. Back it up first. (Read from the CLI's source; not something to try on a directory you care about.)

Any other way of putting `skills/agent-lark/` where your agent loads skills works just as well (`git clone` and copy the folder). To pin a version, install with a git ref (§13).

The CLI is `dist/cli.mjs` inside that directory. Its own messages call it `agent-lark`; an alias makes the commands below shorter (the file is executable, so a symlink into a directory on your `PATH` works too):

```bash
alias agent-lark='node "<path to skills/agent-lark>/dist/cli.mjs"'
```

## 4. Setup

Two things happen once per machine: the Feishu app is created (or adopted) and its credentials are stored. Everything after that — starting the daemon, creating the project's group, flipping the switch — is one command, `away on` (§5).

### 4.1 Scan a QR code (no app yet)

```bash
agent-lark setup
```

Every line of `setup` is printed in Chinese and English side by side. It asks Feishu for a QR-code registration, draws the code in the terminal (as ANSI art) and prints the **same link as a line of text right under it**, so it can be opened even where the drawing renders badly. Scan the code with Feishu on your phone; the confirmation page lists the permissions being requested (§4.3); approve, and the app is created on the spot. The code is valid for a few minutes (the expiry time is printed; a `still waiting for the scan…` line follows once a minute); if it expires, run `setup` again (exit 4). A network hiccup while waiting costs the code: `setup` asks for a fresh one, up to three times.

If your agent runs `setup` for you and cannot show you its screen, it has two ways out: paste you the link line, or render that link into a QR image itself and open it for you; there is no flag for this.

On success:

```
✅ App linked; credentials saved to macOS Keychain (service: agent-lark) (the secret never appears in any output).
Next:
  agent-lark daemon --detach
  cd <project> && agent-lark away on --name "<task>"
```

(the Chinese half of each line is omitted here). Where the credentials go depends on the platform — the OS keychain where one is reachable, else a `0600` file — and can be forced with `--store keychain|file|none` (`none` keeps them in memory for this run only) or `AGENT_LARK_STORE` (§11).

Run `setup` again later and it says `Credentials already exist (from …)` and exits 0. Two flags change that: `--update` rescans to re-authorize the **same** app — that is how a missing scope is added — and `--reset` deletes the stored credentials first, for switching to another app.

### 4.2 Use an app you already have

Skip the QR code and hand over the app id. **The secret is read from the environment or an env file, never from the command line** — argv is visible to every process on the machine:

```bash
AGENT_LARK_APP_SECRET=... agent-lark setup --app-id cli_xxxxxxxx
```

or write both into `~/.config/agent-lark/.env` (`AGENT_LARK_APP_ID=...` / `AGENT_LARK_APP_SECRET=...`) and run `agent-lark setup --app-id cli_xxxxxxxx`. `setup` connects once to check the pair (`✅ Credentials work; app name "…"`), records the app's owner (the person a new group is created for), and stores everything the same way as §4.1. Without a secret it exits 4 and prints both ways; a pair Feishu rejects exits 3. Such an app must have the scopes of §4.3 and the event / callback subscriptions enabled in the Feishu developer console by hand.

### 4.3 Scopes

The confirmation page asks for these scopes (override with `setup --scopes a,b,c`):

```
im:message
im:message:send_as_bot
im:message.group_msg
im:chat
im:resource
im:message.urgent
speech_to_text:speech
```

plus the event `im.message.receive_v1` and the card callback `card.action.trigger`. `im:message.urgent` is what `ask --urgent` needs; `speech_to_text:speech` is what transcribing voice notes needs (and that also needs a paid Feishu tenant, §8). Missing one later? `agent-lark setup --update` rescans and adds it to the same app.

### 4.4 Where credentials are read from

Resolution order, highest first — on a shared machine you want to know which layer wins:

1. `AGENT_LARK_APP_ID` / `AGENT_LARK_APP_SECRET` in the environment (plus `AGENT_LARK_OWNER_OPEN_ID`, see §11)
2. the env file, `~/.config/agent-lark/.env` (`AGENT_LARK_ENV_FILE` to move it); the same three names, or the generic pair below
3. the **OS keychain** — macOS `security`, Linux `secret-tool`, on Windows a DPAPI-encrypted file. This is where `setup` writes by default
4. `~/.config/agent-lark/credentials.json`, mode `0600` (what `setup --store file` writes); a looser mode gets a warning
5. the generic `LARK_APP_ID` / `LARK_APP_SECRET` — last on purpose: several Feishu tools read those names, and a machine running more than one would otherwise cross-wire

`~/.config/agent-lark` is `$XDG_CONFIG_HOME/agent-lark` when that variable is set, and `~/AppData/Roaming/agent-lark` on Windows. `agent-lark status` prints all five layers and marks the one that matched — **and never prints a value**.

## 5. First question, end to end

**Step 1 — turn remote mode on for the project.** In the directory the agent works in (inside the agent's herdr pane if you use herdr, so that pane is recorded):

```bash
cd <project> && agent-lark away on --name "payment refactor"
```

It is a one-stop command: no credentials ⇒ exit 4 and `No Feishu app credentials yet. Run once: agent-lark setup`; starts the daemon if none answers (`daemon: started in the background, pid 12345 (log ~/.agent-lark/daemon.log)`, otherwise `daemon is already running`); waits up to 15 s for the daemon to reach Feishu (exit 3 with the last connection error if it does not); then creates the project's group, named `payment refactor [<project dir>]`, with you in it:

```
Created Feishu group "payment refactor [myproject]"
Remote mode is on: decisions, and moments when the agent is stuck on a prompt that needs you, are pushed to this project's Feishu group.
```

Open Feishu: the group is there. Without `--name` the group is called `[<project dir>]`. A task name is at most 60 characters (code points; exit 1 beyond that).

**When the project used a group before** (it ran `unbind` at the end of an earlier task, or the local records were lost — the daemon also looks through the Feishu groups whose description marks them as this project's), `away on` does not create a second one. It exits 4 and lists the candidates on stderr, one per line — name, when it was let go of, group id — and how to rerun:

```
agent-lark: this project has no live group, but 1 earlier group(s) could be taken back (renamed) instead of creating another:
old task [myproject]  released 2026-09-02T03:04:05.000Z  oc_xxxxxxxx
ask the user which to reuse (rename) or create new; rerun with --reuse <chatId> or --new
```

The choice is yours, not the agent's: `away on --reuse oc_xxxxxxxx --name "payment refactor"` takes the group back and renames it (`Took back Feishu group "…"`); `away on --new --name "…"` creates a fresh one. The agent is told to ask you rather than pick.

**Step 2 — ask yourself a question**, to see the round trip:

```bash
agent-lark ask <<'JSON'
{
  "title":       "Test: which dessert",
  "doing":       "Checking that agent-lark reaches this phone",
  "description": "This is the first question sent through agent-lark from this machine. Nothing depends on the answer.",
  "blocker":     "No blocker; this is a test.",
  "options": [
    {"id": "cake", "label": "Cake", "consequence": "The test passes and you had to think about cake"},
    {"id": "pie",  "label": "Pie",  "consequence": "The test passes and you had to think about pie"}
  ],
  "recommend": "cake",
  "reasoning": "Cake, because it is listed first. The strongest objection is that pie is also good.",
  "question":  "Cake or pie?",
  "lang":      "en"
}
JSON
```

stderr says `note: sent to the Feishu group, waiting for the answer (up to 43200 s)` and the command blocks. The phone shows a blue card titled `🤔 [myproject] Test: which dessert`: the sections **Doing**, **Background**, **Blocker**, a numbered **Options** list (`1. Cake — … ← recommended`), **My recommendation**, **Your call**, then one button per option and a hint line. Tap **Cake** and the terminal prints `Cake`; type `pie, obviously` in the group instead and it prints `pie, obviously` — while a question is pending, the first message you send in that group *is* the answer. The card turns green, `✅ … · Answered`, with your reply on top and the question kept below it; a card nobody answered turns grey (`⌛ … · Timed out` after the timeout, 12 hours by default; `⚠️ … · Cancelled` when the agent gave up or the daemon stopped).

Three variations the agent may use: `"select": "multi"` with `"recommend"` as an array renders checkboxes and a **Submit** button, and the reply is the ticked labels joined with `、`; an option marked `"danger": true` gets a red button behind a confirm dialog and can never be the recommendation; `ask --urgent` flags you in the Feishu app (the card header is red while it waits) and needs the `im:message.urgent` scope — when the flag cannot be delivered the question is still sent and a `note:` says so.

**Step 3 — hand it to the agent.** It reads SKILL.md on its own. Anything you send in the group while no question is pending is injected into the agent's session (herdr) and gets a `Get` reaction once it has landed; a photo or file arrives as `[saved: <absolute path>]` lines so the agent can open it; a voice note is transcribed when the tenant allows it (§8). Replying to one of the cards (Feishu's *reply* action) prefixes the injected text with `(reply to: "<card title>")`, so "yes, do that" keeps its meaning.

**Notifications.** The agent can also send a one-way card that needs no answer:

```bash
agent-lark notify <<'JSON'
{"title": "Build finished", "body": "**Tests**: 483 passed.\n\nNothing to decide; just so you know.", "lang": "en"}
JSON
```

It prints `Notification sent (a reply from the phone is injected into this pane as an instruction)` and returns at once: a light-blue `📣` card, no button, no state — it never changes colour, and replying to it is an instruction like any other message. It is allowed while a question is pending.

**Files.** `agent-lark send-file <path> [--caption <text>]` sends an image (png / jpg / gif / webp / bmp, ≤ 10 MB) or any other file (≤ 30 MB) into the group and prints `Sent to the project group`; the caption goes first as its own message. Only files under the project, under `~/.agent-lark/media` or under the system temp directory can be sent (§9).

**Off, and done.** `agent-lark away off` only flips the switch (`Remote mode is off.`); the group and the binding stay. When the task is over, `agent-lark unbind` lets the group go: it stays in Feishu, and the next `away on` in this directory offers it back (step 1). The daemon keeps running either way.

## 6. Where things live

| path | contents |
|---|---|
| `~/.agent-lark/` | the daemon's state directory (`AGENT_LARK_HOME` or `--home` to move it; created `0700`) |
| `~/.agent-lark/daemon.sock` | the local socket (macOS / Linux). On Windows there is no file: a named pipe `\\.\pipe\agent-lark-<12 hex>` derived from the state directory's path |
| `~/.agent-lark/daemon.pid`, `daemon.log` | pid of the running daemon; a log of ids and state transitions — **never message content** (it does contain project paths and group ids) |
| `~/.agent-lark/bindings.json` | project ↔ group: `root`, `label`, `chatId`, `name`, `paneId` (where phone messages are injected), `away`, `lang` (of the project's last card, used for the daemon's own cards), `boundAt`, `releasedAt` (`null` while the group is the project's live one; the `unbind` time afterwards, kept so it can be offered back) |
| `~/.agent-lark/media/<hash>/` | photos, files and voice notes from the phone, one directory per group. Swept when the daemon starts and every 24 h: files older than `AGENT_LARK_MEDIA_TTL_DAYS` (default 7; `0` switches the sweep off) are deleted; `daemon --status` shows what is kept |
| keychain / `~/.config/agent-lark/` | the app credentials (§4.4) |
| `<project root>/.agent-lark/state.json` | the per-project switch, with a self-ignoring `.gitignore` next to it (content `*`, so your project's own `.gitignore` is never touched). Created by the first `away on` or `bind`; never planted in a project that has not used the skill |

`state.json` holds four fields and nothing else — the injection target stays in `bindings.json`:

```json
{"away": true, "chatId": "oc_xxxxxxxx", "target": "/path/to/project", "updated": "2026-09-15T03:01:52.949Z"}
```

`away status` reads it (`remote mode: on  group: oc_xxxxxxxx`; `--json` prints the file verbatim, or the four fields with `away: false` when the file does not exist); `away`, `bind` and `unbind` write it (`chatId` becomes `null` after `unbind`).

**One thing to know before the first `away on`:** the group's description is set to `agent-lark · <absolute project root>`. That is how a project finds its group again when the local records are gone, and it means the absolute path of your project directory is stored on Feishu's servers, visible to every member of the group. Groups are created with just you in them.

## 7. Day-to-day

```bash
agent-lark rename "second task"          # rename the live group to "second task [<project dir>]"
agent-lark unbind                        # task over: let the group go (kept in Feishu, offered back next time)
agent-lark bind --chat oc_xxxxxxxx       # point the project at a group outright (one you made yourself, or after a reinstall)
agent-lark status                        # credentials, herdr, daemon, every project's group
agent-lark daemon --status               # daemon: pid 12345  connected true  connection connected  pending questions 0  bound projects 1  started …
                                         # media: ttl 7 days, 0.3 MB in 4 files (as of last sweep …)
agent-lark daemon --stop                 # refused (exit 4) while a question is pending; --stop --force cancels it and stops
agent-lark send-file shot.png --caption "current layout"
```

- `rename` works on the live group only (exit 4 without one) and renames it in Feishu (exit 3 when Feishu refuses — the bot may only rename a group it owns or one whose settings let every member edit group info, and must be a member of it).
- `unbind` refuses while a question is pending (exit 4) and exits 1 when nothing is bound.
- `bind` takes the same `--name` / `--reuse` / `--new` as `away on` but does not touch the switch; `--chat <id>` binds that group outright, letting go of the current one (exit 1 if the group is another project's live group; exit 4 while a question is pending). The group's description is rewritten to mark it as this project's.
- `status` shows live bindings (`* marks this project`: root, name, group id, `away=`, `pane=`) and, below them, released groups that could be taken back.
- The daemon does not have to be stopped for a new task, a new group or a context reset; it holds every project's groups at once. Stop it for an upgrade (§13) or to free the machine.

## 8. If something goes wrong

**Exit codes** are the same five everywhere: 0 ok · 1 bad input, nothing sent · 2 timed out, nobody answered (`ask` only) · 3 channel failure (daemon not running, not connected to Feishu, Feishu refused the call; stderr says which) · 4 a human must act. What each command returns:

| command | 1 | 3 | 4 |
|---|---|---|---|
| `ask` | invalid JSON or fields (every problem is listed), bad `--timeout` | daemon down, not connected, send failed | project not bound; a question is already pending (one at a time) |
| `notify`, `send-file` | invalid JSON; file missing, outside the allowed directories, not a regular file, too large | daemon down, not connected, send failed | project not bound |
| `away on` | bad task name, `--reuse` with `--new`, `--reuse` of a group not on offer | daemon did not start, did not reach Feishu within 15 s, creating the group failed | no credentials; earlier groups on offer (list on stderr); the app owner is unknown; Feishu refused the group for lack of a scope |
| `away off`, `away status` | unknown subcommand | `away off`: daemon down | |
| `rename` | no name, name over 60 code points | not connected; Feishu refused (with the permission hint) | no live group |
| `unbind` | nothing bound | daemon down | a question is pending |
| `bind` | as `away on`; `--chat` of another project's live group | as `away on` | as `away on`; `--chat` while a question is pending |
| `daemon --status` | not running | | |
| `daemon --stop` | | the daemon did not stop within 10 s | a question is pending (use `--force`) |
| `daemon --detach`, `daemon` | | already running; did not answer within 10 s | no credentials; `bindings.json` unreadable |
| `setup` | bad `--store` | Feishu rejected the pair; registration failed | no secret for `--app-id`; the QR code expired |

Every failure prints one line on stderr prefixed `agent-lark: `; an unexpected crash exits 3 with its stack trace. `daemon --stop` when nothing runs exits 0 (`daemon: was not running`, removing a stale pid file). `ask` timing out prints `agent-lark: no answer after 43200 s` and exits 2; the card on the phone turns grey.

- **`away on` exits 3 with `daemon is up but not connected to Feishu: …`.** The daemon started but the Feishu handshake failed within 15 s — wrong credentials, no network, Feishu down. `agent-lark daemon --status` shows the last error; the daemon keeps retrying with backoff (5 s doubling up to a minute), so once the cause is fixed simply run `away on` again. `ask` / `notify` / `send-file` against a daemon that has lost its connection exit 3 with `not connected to Feishu (…); the daemon keeps retrying, try again shortly`.
- **Voice notes are saved but not transcribed.** Transcription needs the `speech_to_text:speech` scope **and a paid Feishu tenant**: Feishu's own documentation for the speech-recognition API says the free edition may not call it, and on a free / personal tenant the call fails with HTTP 400 `{"code":99991400,"msg":"request trigger frequency limit"}` even with the scope granted. The voice file is still saved and its path injected, together with a line saying it could not be transcribed and quoting Feishu's error code; type instead. Feishu's documentation for the speech-file recognition API states it is meant for audio of 60 s or less, so keep a voice note under a minute.
- **The card says "read 0/0".** That is Feishu's read counter for messages sent by a bot; it is not a delivery status. The two signs that matter: an answered `ask` card turns green (a `notify` card never changes colour), and a message you sent on your own gets a `Get` reaction once it has reached the terminal — no reaction means it was not injected, and a receipt card says why (§2.2).
- **A message you sent got a receipt card instead of reaching the agent.** The daemon has no pane to inject into (the session that ran the command was not inside herdr, or the recorded pane is gone), herdr is not running on that machine, or the agent is stuck on a prompt only you can answer. Run any `agent-lark` command from the agent's herdr pane to record it again; for the last case, deal with the prompt when you are back.
- **`away on` lists a group you do not want.** Answer with `--new`. The list appears only while the project has no live group, and a group stays on it as long as it is remembered in `bindings.json` or exists in Feishu with the project's marker in its description; there is no command to forget one.

## 9. Security

- **Credentials live in the OS keychain** (macOS `security`, Linux `secret-tool`) or a DPAPI-encrypted file on Windows; `setup --store file` puts them in a `0600` JSON file instead. **The app secret never travels through argv** (`setup --app-id` reads it from the environment or the env file), is never written to any file the skill creates other than those stores, and never appears in any output — `status` marks which layer matched without printing a value.
- **Anyone in the project's group can drive your agent.** A tap answers the question; a typed message becomes an instruction in the agent's session (with herdr). Groups are created with only you in them; keep them that way. The channel does not filter content.
- **Content travels through Feishu's servers**: questions describe your project, photos and files are downloaded from Feishu, and the group's description carries the project's absolute path (§6). Do not put secrets in a question.
- **`send-file` is fenced**: a file is sent only if its real path (symlinks resolved) is under the project root, `~/.agent-lark/media` or the system temp directory; anything else is refused with the three directories listed. This keeps the agent from mailing arbitrary files off the machine.
- **The daemon log records ids and events only** — `ask.sent`, `inject`, `bind` … with project paths, group ids and request ids — never the text of a question, a reply or a message.
- To start over with a different app: `agent-lark setup --reset` (deletes the stored credentials, then registers by QR code). Delete the old app in the Feishu developer console if it should stop working.

## 10. Known limits

- Linux and Windows have unit-test coverage only, no end-to-end run in a real environment (§2.1). Pull requests with a real-machine report are welcome.
- **One pending question per project.** A second `ask` exits 4 while the first is waiting; a typed reply cannot be tied to a specific card, so concurrency is not attempted. Different projects do not block each other.
- One custom Feishu app serves one tenant, and one machine stores one set of credentials: a work account and a personal account cannot both be wired up at once.
- Stopping the daemon cancels every pending question (the cards turn grey, the waiting `ask` exits 3). `--stop` refuses while any is pending unless you pass `--force`.
- Card limits: title ≤ 200 characters on one line; `doing` / `description` / `blocker` / `reasoning` / `question` ≤ 4000 characters each; 2–5 options with labels ≤ 60 and consequences ≤ 500 characters; `notify` body ≤ 8000 characters. Over-length input is rejected with the field named, never truncated.
- A task name (`--name`, `rename`) is at most 60 code points.
- Phone → agent injection needs herdr (§2.2). The daemon does not check whether the agent is busy (your CLI queues the input); when herdr reports the agent as stuck on a prompt it sends a receipt instead.
- Voice notes: 60 s per note (the limit Feishu's speech-file recognition API documents), and transcription only on paid tenants (§8).
- The 🔔 *waiting for you* card is pushed at most once a minute per project and only while `away` is on.

## 11. Environment variables

| variable | default | effect |
|---|---|---|
| `AGENT_LARK_HOME` | `~/.agent-lark` | the daemon's state directory (§6). `--home <dir>` on the command line overrides it and is handed to a daemon started with `--detach`. With the Unix-socket transport keep the path short: a socket path over the system limit makes every command fail with `connect EINVAL …/daemon.sock` |
| `AGENT_LARK_APP_ID`, `AGENT_LARK_APP_SECRET` | | the app credentials, first in the resolution order (§4.4). The pair in the environment wins over the keychain |
| `AGENT_LARK_OWNER_OPEN_ID` | | the app owner's `open_id`, needed to create a group when the credentials come from the environment or the env file (the keychain entry written by `setup` records it). Without it `away on` in a project with no group exits 4 (`nobody to invite into a new group`) — bind an existing group with `--chat` instead |
| `AGENT_LARK_ENV_FILE` | `~/.config/agent-lark/.env` | the env file (§4.4) |
| `AGENT_LARK_STORE` | `keychain` where one is reachable, else `file` | where `setup` writes: `keychain`, `file` (`~/.config/agent-lark/credentials.json`, `0600`) or `none` (memory only for that run). `setup --store` overrides |
| `AGENT_LARK_KEYCHAIN` | `agent-lark` | keychain service name (macOS and Linux; the account is `app`) |
| `AGENT_LARK_MEDIA_TTL_DAYS` | `7` | how many days inbound photos, files and voice notes are kept under `~/.agent-lark/media`; `0` switches the sweep off. A value that is not a whole number is refused with a warning and the default is used |
| `XDG_CONFIG_HOME` | | moves `~/.config/agent-lark` (env file and credentials file) as on any XDG-aware tool |
| `LARK_APP_ID`, `LARK_APP_SECRET` | | the generic pair, last in the resolution order (§4.4) |
| `HERDR_ENV`, `HERDR_PANE_ID` | set by herdr | detected, never set by you: inside herdr, `away on` / `away off`, `bind`, `rename`, `ask`, `notify` and `send-file` record the current pane on the project's binding, and phone messages are injected there (`unbind`, `status`, `away status` and `daemon` do not touch it) |

There is no language variable: the fixed wording of a card follows the `lang` field of the question or notification that produced it (`zh` by default); the daemon's own cards (receipts, the 🔔 card) follow the project's last `lang`, English before any; everything the agent reads — stdout, stderr, `help` — is English; `setup` prints both.

## 12. CLI reference

Output of `agent-lark help` (also `--help`, `-h`, or no arguments):

```
agent-lark — reach the agent session running in your terminal from Feishu/Lark

  setup [--update] [--scopes a,b]  Create or update the Feishu app by QR code; credentials go to the keychain
  setup --app-id cli_xxx [--store]  Use an existing app; the secret is read from the environment / env file, never argv
  setup --reset                      Forget the stored credentials and set up again from scratch in the same run (QR code or --app-id)
  daemon [--detach|--status|--stop]  Resident process holding the Feishu connection (--stop is refused while a question is pending, unless --force)
  away on [--name <task>] [--reuse <chat_id> | --new]
                                     Remote mode on: daemon up, this project bound to a Feishu group named "<task> [<dir>]"
                                     (exit 4 lists earlier groups to take back; rerun with --reuse or --new)
  away off | status [--json]         Remote mode off / the project's state ({away, chatId, target, updated})
  rename "<task>"                    Rename the project's live group to "<task> [<dir>]"
  unbind                             Let the live group go (it stays in Feishu; the next away on offers it back)
  bind [--chat <id>] [--name <task>] [--reuse <chat_id> | --new]
                                     Bind without switching remote mode on; --chat names a group outright
  ask [--timeout <seconds>] [--urgent]
                                     Read JSON from stdin, push a question card, block until answered (--urgent flags the owner in-app)
  notify                             Read JSON from stdin, push a titled notification card (important things only)
  send-file <path> [--caption <t>]   Send an image or file to the project group
  status                             Daemon and binding overview

Global: --home <dir>  state directory (same as AGENT_LARK_HOME; default ~/.agent-lark)

Exit codes: 0 ok · 1 bad input · 2 timed out, nobody answered · 3 channel failure · 4 a human must act
```

`--home <dir>` (or `--home=<dir>`) goes anywhere on the command line. Command by command:

- **`setup [--update] [--reset] [--scopes a,b] [--store keychain|file|none]`** — create the app by QR code (§4.1). `--update` rescans for the app already stored (to add scopes or re-authorize); `--reset` deletes the stored credentials first; `--scopes` replaces the default list (§4.3). Prints every line in Chinese and English.
- **`setup --app-id cli_xxxxxxxx [--store …]`** — adopt an existing app; the secret comes from `AGENT_LARK_APP_SECRET` (or `LARK_APP_SECRET`, or the env file) (§4.2).
- **`daemon`** — run the daemon in the foreground (`agent-lark daemon: pid 12345, listening at ~/.agent-lark/daemon.sock, connecting to Feishu in the background`); Ctrl-C stops it. **`--detach`** starts it in the background and waits up to 10 s for it to answer; **`--status`** prints the two lines shown in §7 (plus `last error: …` when the connection failed); **`--stop [--force]`** asks it to stop and waits up to 10 s. Never start the daemon as a background job of the agent's own shell: it would die with the agent.
- **`away on [--name "<task>"] [--reuse <chat_id> | --new]`** — §5 step 1. **`away off`** flips the switch off. **`away status [--json]`** prints the project's `state.json` (§6) without talking to the daemon.
- **`rename "<task>"`**, **`unbind`**, **`bind [--chat <id>] [--name "<task>"] [--reuse <chat_id> | --new]`** — §7.
- **`ask [--timeout <seconds>] [--urgent]`** — reads one JSON object from stdin (a heredoc; a terminal on stdin is refused), validates it before anything is sent, pushes the card and blocks. The reply is printed on stdout: the tapped label, the ticked labels joined with `、`, or the typed message verbatim. The default timeout is 43 200 s (12 hours). The field contract is in [SKILL.md](SKILL.md) and [references/message-spec.md](references/message-spec.md).
- **`notify`** — reads `{"title", "body", "lang"}` from stdin; body is Markdown. Prints `Notification sent (…)`.
- **`send-file <path> [--caption <text>]`** — §5 *Files*.
- **`status`** — credentials (which of the five layers matched), `herdr: inside herdr, pane wG:p3` / `not inside herdr`, the daemon line, then every project's live group and the released ones.

The stderr text of each failure is in [references/failures.md](references/failures.md); how the daemon behaves, in [references/daemon.md](references/daemon.md).

## 13. Versions and upgrading

Versions are git tags `agent-lark/vX.Y.Z` (the repository holds two skills; each has its own tags); what changed is in [CHANGELOG.md](../../CHANGELOG.md). 0.1.0 is the first release, so there is nothing older to upgrade from yet; the procedure below is for the releases after it. The `skills` CLI and skills.sh do not read a version number — an install is a snapshot of the repository content, and `npx skills update` refreshes it (`-g` for global installs, `-p` for the current project). To stay on a release, install with the tag as git ref; per the `skills` CLI documentation `update` then stays on that ref:

```bash
npx skills add 'yezhoujie/agent-ntfy-skill#agent-lark/v0.1.0' --skill agent-lark
```

**Upgrading a machine that already runs a daemon** — do the steps in this order:

1. Stop the running daemon **with the CLI you have now**: `agent-lark daemon --stop`. It refuses while a question is pending; wait for the answer or use `--stop --force` (the pending card turns grey). If you already replaced the files and the old daemon does not answer the stop request, send it `kill -TERM <pid>` (the pid is in `~/.agent-lark/daemon.pid`).
2. Update the files: `npx skills update` (or run the install command again, or copy the directory).
3. Start the new daemon: `agent-lark daemon --detach`, then `agent-lark daemon --status`. Every project's group is still bound: `bindings.json` is read by the new daemon as it is.
4. In the herdr pane your agent works in, run `agent-lark away on` (or `ask`, `notify`, `send-file`, `rename`, `bind`) so the binding records that pane again — `status` and `away status` do not record anything.

## 14. Integration: keeping the skill in force for the whole session

The skill only provides the calls — `ask`, `notify`, `send-file`, `away`, `rename`, `unbind` — and deliberately never decides *when* to use them (SKILL.md, "When to use"). Left alone, an agent uses agent-lark only when it happens to remember the skill exists, which is not what you want while you are away. The trigger policy belongs in the agent's **standing instructions** — the file it loads in every session — and it has to cover four moments:

1. **Session start / context reset**: read `<project root>/.agent-lark/state.json` (`away status --json`); `away: true` means the human is away and every decision goes to the phone from now on.
2. **The human leaves** ("I'm leaving, send it to my phone"): run `away on --name "<task>"` while they are still at the keyboard and relay its output; when it exits 4 with earlier groups on offer, ask which to reuse — never pick one.
3. **While away**: every question, confirmation or authorization becomes an `ask` (run in the background, one at a time, act on the exit code; `--urgent` only for irreversible actions or short timeouts); phone messages arrive with the `[agent-lark remote] ` prefix; `notify` is reserved for answering a question asked from the phone and for major events that need no decision — task finished, an error, the task cannot continue — never for progress chatter.
4. **The human is back**: `away off`; when the task is over, `unbind`. The daemon keeps running.

A ready-made rule that does exactly this ships with the skill: [`examples/remote-mode-rule.md`](examples/remote-mode-rule.md) (English) and [`examples/remote-mode-rule.zh-CN.md`](examples/remote-mode-rule.zh-CN.md) (Chinese). It also covers teams of agent sessions (only the session that talks to the human holds remote mode). For Claude Code, rules in `~/.claude/rules/` are injected into every session:

```bash
cp ~/.claude/skills/agent-lark/examples/remote-mode-rule.md ~/.claude/rules/agent-lark-remote-mode.md
```

For other agents, put it wherever that agent loads its standing instructions. Adjust the `<skill dir>` path at the top and the trigger phrases ("I'm leaving", "I'm back") to your own habits; the rest is product behaviour and should stay as written.

**Both skills installed?** They do not know about each other, and nothing in either decides which one a decision goes to. That is the rule's job: keep one rule in force per machine (or per project), and let it name the CLI it calls. Running both daemons side by side is fine; they share nothing.
