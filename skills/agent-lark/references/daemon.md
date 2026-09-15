# The daemon, groups and bindings, environment

`agent-lark` below means `node <skill dir>/dist/cli.mjs`.

## Contents

1. What the daemon is
2. Starting it (and how not to)
3. Status, stop, lifecycle
4. Groups and bindings
5. Messages from the phone when no question is pending
6. Attachments and the media directory
7. Environment variables and files
8. The per-project state file
9. Language of the fixed wording

## 1. What the daemon is

One resident process holds the **only** Feishu connection (a WebSocket the SDK keeps open — no public
URL, no webhook, no inbound port) and the only copy of the state. It listens to every group bound to a
project, routes each incoming message either to the `ask` that is waiting for that project or (no
question pending) into the project's terminal pane, rewrites the card when a question is answered,
times out, or is cancelled, and pushes the alert cards it sends on its own initiative.

Everything else (`ask`, `notify`, `send-file`, `bind`, `unbind`, `rename`, `away`, `status`, `daemon
--status|--stop`) is a thin client that talks to the daemon over a local IPC endpoint and holds nothing.
The endpoint is a Unix domain socket on POSIX (`<home>/daemon.sock`) and a named pipe on Windows
(`\\.\pipe\agent-lark-<12 hex chars derived from the state directory>`; no file). `ask` keeps its
connection open until the answer arrives; if the daemon dies, the connection drops and `ask` exits 3 at
once. No silent hang. Only `setup` never talks to the daemon.

## 2. Starting it (and how not to)

`ask`, `notify` and `send-file` exit 3 and name the start command when the daemon is not running:

```
agent-lark: daemon is not running. Start it first: agent-lark daemon --detach
```

You may start it for the user. The only rule: **it must outlive you.**

| where you are | do this |
|---|---|
| anywhere (macOS, Linux, Windows, inside or outside herdr) | `agent-lark daemon --detach`: starts the daemon in its own session with stdout and stderr appended to `<home>/daemon.log`, waits up to 10 s for the endpoint to answer, prints `daemon: started in the background, pid N (log <home>/daemon.log)` (rc 0). `away on` runs exactly this for you when nothing is listening |
| a terminal the human keeps open | `agent-lark daemon` (foreground): prints `agent-lark daemon: pid N, listening at <endpoint>, connecting to Feishu in the background` and stays; Ctrl-C stops it cleanly |

**Never** start it as a background job of your own shell, under a Monitor, in a subagent, or with `&`
in a tool call: those die with your session, and every message the human sends afterwards is lost
without any error on their side.

What can go wrong at start (foreground: one stderr line and that exit code; `--detach`: the line lands
in the log and `--detach` reports `daemon started but did not answer within 10 s; see the log: <home>/daemon.log`, rc 3):

| stderr / log line | rc | meaning |
|---|---|---|
| `no Feishu app credentials found. Run agent-lark setup first (or set AGENT_LARK_APP_ID / AGENT_LARK_APP_SECRET)` | 4 | nothing in any credential layer (§7) |
| `daemon is already running` | 3 (foreground) · 0 (`--detach`, nothing started) | something already answers on the endpoint |
| `cannot read the bindings file <home>/bindings.json: <error>` + `Fix or move it; it is left untouched.` | 4 | the file exists but is not readable JSON; the daemon never replaces it with an empty one |
| `agent-lark: warning: AGENT_LARK_MEDIA_TTL_DAYS=<value> is not a whole number of days; using 7` | (warning only) | the retention setting was ignored, §6 |

## 3. Status, stop, lifecycle

```
agent-lark daemon --status   # rc 0 + two lines:
                             #   daemon: pid N  connected true  connection connected  pending questions 0  bound projects 1  started 2026-09-15T04:03:11.902Z
                             #   media: ttl 7 days, 0.0 MB in 0 files (as of last sweep 2026-09-15T04:03:12.010Z)
                             # a third line "  last error: <reason>" while Feishu is not reachable
                             # rc 1 (on stderr): "agent-lark: daemon: not running", or "agent-lark: daemon: no answer (<reason>)"
agent-lark daemon --stop     # rc 0 "daemon: stopped" (or "daemon: was not running", which also removes a stale pid file)
                             # rc 4 while a question is pending (see below); --force overrides
                             # rc 3 "daemon: still answering 10 s after the stop request; see the log: <home>/daemon.log"
```

- **The daemon is up before it reaches Feishu.** It opens the IPC endpoint first and runs the Feishu
  handshake in the background: a failed attempt is recorded (`last error:`), retried after 5 s, then
  10, 20, 40, 60 s and every 60 s from there; the process never exits over it. Until `connected true`,
  the commands that need Feishu (`ask`, `notify`, `send-file`, `rename`, and a `bind` that has to create
  or look up a group) answer rc 3 `not connected to Feishu (<last error>); the daemon keeps retrying, try again shortly`;
  `ping`, `status`, `unbind`, `away off`, `bind --chat` and `--stop` work regardless. Once the first
  handshake succeeded the SDK's own reconnect takes over: a dropped connection shows as `connected false`
  with `last error: the connection to Feishu dropped, reconnecting` until it is back.
- `--status` is a probe over the endpoint (5 s). `connection` is the SDK's own state word
  (`connecting` until the first handshake, then `connected`, …); `connected` is the daemon's yes/no.
- **`--stop` refuses while a question is pending** (rc 4):
  ```
  agent-lark: 1 question(s) still pending on the phone. Stopping the daemon now turns those cards into "⚠️ Cancelled" — a dead card for the human.
  Wait for the answer, or do it anyway: agent-lark daemon --stop --force
  ```
  With `--force` (or on SIGINT / SIGTERM / SIGHUP, which stop it the same way) every waiting `ask` gets
  rc 3 `the daemon is stopping; the question was sent but no answer will arrive this time`, each card is
  rewritten to `⚠️ … · Cancelled` (3 s allowed per card, then it is left as it was), Feishu is
  disconnected, open IPC connections get 2 s to drain, and `daemon.sock` / `daemon.pid` are removed.
- Files under `AGENT_LARK_HOME` (default `~/.agent-lark/`, directory mode 0700):

  | file | content |
  |---|---|
  | `daemon.sock` | the listening socket (POSIX); removed on exit. On Windows the pipe name vanishes with the process, no file |
  | `daemon.pid` | the daemon's pid; removed on exit |
  | `daemon.log` | one line per event, `<ISO time> <event> <JSON detail>` (ids, states, error codes; never message bodies, never credentials); stderr of a detached daemon lands here too |
  | `bindings.json` | every group ever bound, live and released (§4); mode 0600 |
  | `media/` | attachments the human sent, one subdirectory per group (§6) |

- A stale `daemon.sock` from a crash is removed at the next start (POSIX); a pid file with no daemon
  behind it is removed by `daemon --stop`.

## 4. Groups and bindings

The unit is the **project**: the git toplevel of the directory a command runs in, else that directory
(a worktree or a submodule is its own project). One project has **one live group** at a time; a group is
never shared by two projects. The group is created by the bot with the app owner invited, named
`<task> [<dir>]` (`[<dir>]` alone when no `--name` was given; `<dir>` is the project directory's name),
and its description is set to `agent-lark · <project root>` — that marker is how the group is found again
when the local records are gone. Task names are at most 60 characters (code points; longer exits 1
`task name: over 60 characters (code points), got N`).

`<home>/bindings.json` is written by the daemon only:

```json
{
  "bindings": [
    {
      "root": "/Users/me/work/my-project",
      "label": "my-project",
      "chatId": "oc_xxxxxxxx",
      "name": "payments refactor [my-project]",
      "paneId": "w1:p2",
      "away": true,
      "lang": "en",
      "boundAt": "2026-09-15T04:03:11.902Z",
      "releasedAt": null
    }
  ]
}
```

| field | meaning |
|---|---|
| `root` / `label` | the project and its directory name (the `[label]` in card titles) |
| `chatId` | the Feishu group; the key — one entry per group |
| `name` | the group name as last set here; `null` when never set by us |
| `paneId` | the herdr pane phone messages are injected into; refreshed by every command run with `HERDR_PANE_ID` set (`ask`, `notify`, `send-file`, `bind`, `rename`, `away on|off`) |
| `away` | remote mode switch as the daemon knows it (the stuck alert only polls `away` bindings) |
| `lang` | `lang` of the project's last `ask` / `notify`; the receipt and alert cards follow it (§9) |
| `boundAt` / `releasedAt` | ISO times; `releasedAt: null` marks the live group, a time means `unbind` let it go (kept so it can be offered back) |

**How `bind` (and `away on`, which is `bind` + switch) picks the group** (the stdout lines quoted below
are `away on`'s; `bind` itself prints `✅ Already bound to Feishu group "…" (<root>)` / `✅ Took back … for <root>` /
`✅ Created … and bound it to <root>` / `✅ Bound to existing group oc_… (<root>)`, see [failures.md](failures.md) §8):

1. `--chat <id>` names a group outright — the escape hatch for a group the human created themselves, or
   for pointing a project back at its group after a reinstall. The group's name and description are
   updated to this project (`note: not connected to Feishu; the group's name and description were left as they are`
   when offline; `note: bound, but renaming the group failed: …` when Feishu refuses). Another project's
   live group is refused (rc 1 `group oc_… is the live group of another project (<root>); unbind it there first`);
   switching away from this project's own live group while a question is pending is refused like `unbind` (rc 4).
2. The project has a live group ⇒ it is kept (`Connected to Feishu group "…"`); `--name` renames it in the
   same call; `--reuse` / `--new` are ignored with a `note:`.
3. No live group ⇒ **candidates** are collected: groups this project released earlier (on record) plus
   groups in Feishu whose description is exactly this project's marker but that are on no record (a lost
   `bindings.json`). Looking through Feishu can fail (`note: could not look through the Feishu groups for earlier ones of this project (<error>); only local records were considered`).
   - candidates exist and neither `--reuse` nor `--new` was given ⇒ **rc 4**, the list on stderr
     (`<name>  released <time>  <chatId>`, `-` for a group that was never released locally) and
     `ask the user which to reuse (rename) or create new; rerun with --reuse <chatId> or --new`. The choice
     is the human's; relay the list.
   - `--reuse <chatId>` ⇒ that group becomes live again, renamed to `<task> [<dir>]` with the description
     re-pointed at this project (`Took back Feishu group "…"`). A `chatId` that is not a candidate exits 1.
   - `--new`, or no candidates ⇒ a new group is created (`Created Feishu group "…"`). The app owner must
     be recorded (rc 4 `nobody to invite into a new group (the app owner is not recorded). Use --chat <chat_id> to bind a group you created yourself.`
     otherwise: `setup --update` records it). Creation refused for a permission reason is rc 4, any other
     failure rc 3, both `creating the group failed: <error>` with the `setup --update` / `--chat` hint.

`unbind` (rc 1 `this project is not bound`; rc 4 `a question is still pending on the phone; answer it or wait for the timeout`)
sets `releasedAt`, switches `away` off on that binding, and makes the daemon stop listening to the
group; the group itself is untouched in Feishu. `rename "<task>"` renames the live group (rc 4
`this project has no live group; run agent-lark away on first`; rc 3 with Feishu's code when refused —
only the bot's own groups can be renamed freely, a human-made group only when its settings let every
member edit group info; codes 232002 / 232016 / 232011 get that hint appended).

`status` lists every binding: `bindings:` with one `  * <root>  <name>  <chatId>  away=<bool>  pane=<pane>` line
per live group (`*` marks the current project), then `released (take one back with away on --reuse <chat_id>; * marks this project):`
with `  <root>  <name>  <chatId>  released <time>` lines.

## 5. Messages from the phone when no question is pending

A message in a bound group with no question pending for that project is injected into the project's
terminal as one prompt: `[agent-lark remote] <text>` — the prefix is protocol and never translated; the
text after it is the human's, unchanged (attachments and voice notes are turned into text first, §6).
The target pane is the binding's `paneId`; when none was ever recorded, the daemon looks for a pane
whose agent runs in the project directory (`herdr agent list`, the focused one first). The prompt is
delivered with `herdr agent prompt <pane> "<text>"`; on success the daemon adds a `Get` reaction to the
human's message.

When it cannot be delivered, the human gets an orange **`⚠️ [<dir>] Not delivered`** card in the group
(`That message never reached the terminal: <why>`), and you see nothing:

| why | wording on the card (`en`) |
|---|---|
| no pane on record and none found | `No herdr pane is recorded for this project, so there is nowhere to deliver the message.` |
| herdr answered `agent_blocked` (your session sits on a prompt only a human can answer) | `The agent in the terminal is stuck on a prompt only you can answer and cannot take new input. Deal with it when you are back at the computer.` |
| herdr answered `agent_not_found` / `pane_not_found` | `The recorded herdr pane is gone. Run agent-lark away on (or any agent-lark command) inside the project to record the pane again.` |
| `herdr` is not installed or not running | `This machine has no herdr, or herdr is not running; messages from the phone have nowhere to go.` |
| any other herdr error | `herdr refused the injection: <code> <message>` |

If the human tells you they sent something you never received, the receipt card on their phone and
`<home>/daemon.log` (`inject` events with the herdr code) are where to look.

**The stuck alert.** Every 5 s the daemon runs `herdr agent list` for the bindings that have `away: true`
and a pane. When a session's status changes *to* `blocked` (a permission prompt, a choice dialog — herdr's
own judgement) and no question of that project is pending, it sends an orange **`🔔 [<dir>] waiting for
you`** card with the pane's terminal title and pane id, at most once per 60 s per project. Idle and
finished sessions never trigger anything. Outside herdr `paneId` is never recorded, so nothing is polled.

## 6. Attachments and the media directory

Images, files, video and voice notes the human sends are downloaded before injection into
`<home>/media/<12 hex chars per group>/<epoch ms>-<file name>` (a voice note becomes `…-audio-<epoch>.opus`)
and listed one per line as `[saved: <absolute path>]`, then `(attachments saved locally)` (or
`(I sent attachments; they are saved locally)` when the message had no text). A download that fails is
logged and skipped, so the line count can be lower than the attachment count.

A voice note is also transcribed (Feishu's speech recognition, opus, up to 60 s). A voice message on its
own is injected as the transcript, plain (the `<audio …/>` placeholder is stripped, nothing marks the text
as spoken); when the message also carries text, the transcript follows it as `(voice transcript) <text>`.
When Feishu refuses, the injected line carries the
code and message and names the two likely causes (missing `speech_to_text:speech` scope · a free-plan
tenant, which cannot call speech recognition at all); when the call succeeds but nothing was recognised,
`(N voice message(s) received but nothing was recognised in the audio. …)`. The `[saved:]` line is
there in every case.

Retention: at start and every 24 h the daemon deletes files under `media/` older than 7 days
(`AGENT_LARK_MEDIA_TTL_DAYS=<days>`; `0` switches the sweep off), then the directories left empty;
symlinks are never followed. `daemon --status` reports the setting and what the directory held at the
last sweep. `send-file` may send anything from this directory back (the media directory is on its
allowlist), which is how a file the human sent can be returned edited.

## 7. Environment variables and files

| variable | default | effect |
|---|---|---|
| `AGENT_LARK_HOME` | `~/.agent-lark` | the daemon's state directory (§3). `--home <dir>` (or `--home=<dir>`, anywhere on the command line) sets it for that command and for a daemon it starts, and the interactive `setup --reuse` handed to a new pane is given it explicitly |
| `AGENT_LARK_APP_ID` / `AGENT_LARK_APP_SECRET` | – | app credentials from the environment, for one process: a runtime override that wins over the stores (below). The only way to supply credentials besides `setup` |
| `AGENT_LARK_OWNER_OPEN_ID` | – | the app owner's `open_id` when credentials come from the environment; needed to create groups and for `--urgent` (`setup` records it in the store on its own, from the QR registration or from the probe of a reused app) |
| `AGENT_LARK_STORE` | `keychain` where one is usable, else `file` | where `setup` writes: `keychain` (macOS Keychain · Windows DPAPI-encrypted file `<config>/credentials.dpapi` · Linux `secret-tool`), `file` (`<config>/credentials.json`, mode 0600), `none` (this run only) |
| `AGENT_LARK_KEYCHAIN` | `agent-lark` | the keychain service name (`security` on macOS, `secret-tool` on Linux) |
| `AGENT_LARK_MEDIA_TTL_DAYS` | `7` | retention of `<home>/media/`; `0` = keep everything (§6) |
| `AGENT_LARK_OFFLINE` | – | testing / offline only: `1` makes `setup` refuse to contact Feishu (rc 3 `offline: refusing to contact Feishu (AGENT_LARK_OFFLINE=1 is set)`) before the QR registration or the credential probe; the test runner sets it so no test can register an app by accident. Not for normal use |
| `XDG_CONFIG_HOME` | `~/.config` (`%AppData%` on Windows) | `<config>` above is `$XDG_CONFIG_HOME/agent-lark` |
| `HERDR_ENV`, `HERDR_PANE_ID` | set by herdr | detected, not configured: `HERDR_PANE_ID` is recorded on the binding as the injection target and is the pane a handed-off `setup --reuse` reports back to; `HERDR_ENV=1` is what `status`, `away on` and `setup --reuse` mean by "inside herdr" |

Credentials are resolved in this order, highest first; `status` shows which layer won and marks every
layer (`✓` present, `·` absent):

1. `AGENT_LARK_APP_ID` / `AGENT_LARK_APP_SECRET` in the environment
2. the OS keychain (what `setup` writes by default)
3. `<config>/credentials.json` (what `AGENT_LARK_STORE=file`, or a platform without a keychain, writes; a warning is printed when its mode is looser than 0600)

Nothing else is read: no env file, no unprefixed `LARK_*` names. The App Secret is typed by the human
into the interactive `setup` (echo off) and never travels through argv (`ps` shows argv to every user on
the machine), a file the CLI writes other than the stores above, or any output — a probe error that
quotes it is printed with the secret masked as `***`.

## 8. The per-project state file: `<project root>/.agent-lark/state.json`

Written by the CLI (never by the daemon), read by the agent and by whatever rule the user keeps about
remote mode. Project root as in §4. The directory carries its own `.gitignore` (`*`), so git never sees
it and the project's own `.gitignore` is not touched.

| field | written by | meaning |
|---|---|---|
| `away` | `away on` (`true`) · `away off` / `unbind` (`false`) | the human is away and wants decisions on the phone |
| `chatId` | `bind` / `away on` (the group) · `unbind` (`null`) | the group this project is bound to right now |
| `target` | every write | the project root, informational |
| `updated` | every write | UTC time, ISO 8601 |

`bind` and `away on` create the directory; `away off` and `unbind` only update a file that already
exists, so a project that never used the channel gets no directory. `away status` prints the file in
words (`remote mode: on  group: oc_…`; `This project has never used agent-lark (no .agent-lark/state.json)`
when there is none); `away status --json` prints it verbatim, or `{"away":false,"chatId":null,"target":"<root>","updated":""}`
when there is none. The file is not checked against the daemon: `status` is the command that asks the
daemon. Writes are atomic (temp file + rename) but unlocked. No credential and no pane id is ever
written here.

## 9. Language of the fixed wording

- **Cards**: the `lang` field of the `ask` JSON selects the wording of that card and of all its later
  states (section labels, `← recommended`, hints, `Submit`, confirm dialogs, `Answered` / `Timed out` /
  `Cancelled`); **omitted means `en`**. A `notify` card has no fixed wording at all (title and body only).
  The cards the daemon sends on its own — the receipt (§5) and the stuck alert (§5) — follow the `lang`
  of the project's most recent `ask` or `notify`; before any, `en`.
- **Everything the agent reads** — CLI stdout and stderr, validation reports, `note:` lines, the injected
  text, `help` — is English only, and nothing in the environment changes that.
- **`setup`** prints every line in Chinese and English side by side, since a human is at the terminal for it.
- The `[<dir>]` prefix in card titles, the `[agent-lark remote] ` injection prefix and the `[saved: …]` /
  `(voice transcript)` / `(reply to: …)` / `(follow-up) …` lines are protocol, not wording.
