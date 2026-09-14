# The daemon, slots, and environment

`agent-ntfy` below means `python3 <skill dir>/scripts/agent_ntfy.py` (on Windows the interpreter is `python`, as the test workflow runs it).

## Contents

1. What the daemon is
2. Starting it (and how not to)
3. Status, stop, lifecycle
4. Slots and leases
5. The reachability gate (`confirm-sub`)
6. Messages from the phone when no question is pending
7. Environment variables and files
8. Language of the fixed wording

## 1. What the daemon is

One resident process is the **only** ntfy subscriber and the only holder of state. It keeps a single
connection open for every topic in the pool, routes each incoming message either to the `ask` that is
waiting on that slot or (no question pending) into the target agent's session, and rewrites the phone
card when a question is answered, times out, or is cancelled.

Everything else (`ask`, `notify`, `slots`, `release`, `confirm-sub`, `add-slot`, `away`) is a thin client
that talks to the daemon over a local IPC endpoint and holds nothing. The endpoint is a Unix domain socket
on POSIX and a loopback TCP port on Windows (`AGENT_NTFY_IPC`, §7). `ask` keeps its connection open until
the answer arrives; if the daemon dies, the connection drops and `ask` exits 3 at once. No silent hang.

## 2. Starting it (and how not to)

`ask` and `notify` exit 3 and print the start commands when the daemon is not running. You may start it
for the user. The only rule: **it must outlive you.**

| where you are | do this |
|---|---|
| inside herdr (exercised on macOS; the pane command is a POSIX `env …` line, not tried on Windows) | `agent-ntfy away on` starts it in a new pane for you — and switches remote mode on (SKILL.md, "Remote mode"). By hand: `herdr pane split --current --direction right --cwd "$PWD" --no-focus` returns the new pane id (`.result.pane.pane_id`); then `herdr pane run <pane id> "python3 <skill dir>/scripts/agent_ntfy.py daemon"`. Visible, and herdr owns its lifetime |
| macOS / Linux, outside herdr | `agent-ntfy daemon --detach`: starts the daemon in its own session, prints `daemon: started in the background, pid N (log <home>/daemon.log)` once the endpoint answers |
| Windows, outside herdr | `agent-ntfy daemon --detach`: starts it as a detached background process (no console window, by the `DETACHED_PROCESS` flag); same output. Run it from Git Bash like every other command in these docs (under WSL the skill runs as Linux) |

**Never** start it as a background job of your own shell, under a Monitor, in a subagent, or with `&`
in a tool call: those die with your session, and every message the human sends afterwards is lost
without any error on their side.

A second start is refused (`a daemon is already running`, rc 3); check with `--status` first. If
`--detach` reports `daemon (pid N) not ready within 5 s, still starting; check later with agent-ntfy daemon --status, log <home>/daemon.log`
(rc 3), the process is up but its endpoint did not answer in time: read the log, then `--status`. A
child that exited immediately is reported as `the daemon did not come up (exit code N), see <log>` (rc 3).

## 3. Status, stop, lifecycle

```
agent-ntfy daemon --status    # rc 0 + one line:  daemon: pid N  subscription: connected  pending questions: 0  confirming: 0  slots: 5  transport: unix
                              # rc 1: "daemon: not running", or "daemon: no answer (pid file N still there; it may have died)"
agent-ntfy daemon --stop      # asks the daemon over the endpoint to shut down, then waits up to 30 s for its files to go; rc 0 "daemon: pid N stopped"
```

- `--status` is a probe over the endpoint; `transport:` is `unix` or `tcp`. A pid file with no answer
  means the process died without cleaning up, or is stuck: look at the log.
- `--stop` sends the `stop` request and waits for the endpoint file to disappear; no signal is sent on
  any platform. rc 1 with `did not acknowledge the stop` or `did not exit within 30 s` means the daemon
  answered but did not finish; a daemon that is not running gives rc 1 when a stale pid file exists and
  rc 0 when there is nothing at all. Stopping can take a few seconds: in-flight injections get a grace
  period and undelivered receipts are published on a best-effort basis (5 s each; failures are only logged).
- Ctrl-C in the daemon's own pane, or SIGTERM / SIGHUP from an external tool on POSIX, shut it down the
  same way (the handlers stay installed). On Windows the handlers cover Ctrl-C (and Ctrl-Break where the
  interpreter provides `SIGBREAK`) in the daemon's own console; a daemon started with `--detach` has no
  console, so stop it with `--stop`.
- Subscription states: `connected` / `connecting` (just started) / `down for N s` (reconnecting with backoff 1 → 30 s). After 3 consecutive failures or 60 s down, every waiting `ask` gets a `note:` line on stderr; another when it is back.
- **Cold start does not replay.** Only messages arriving after the connection is up are seen; a question does not survive a daemon restart (its `ask` already exited 3).
- **Stopping** ends every waiting `ask` with rc 3 (`the daemon is stopping; the question went out …`). The cards on the phone are left as they are: the human may still answer, and that answer will be injected after the restart.
- Files under `AGENT_NTFY_HOME` (default `~/.agent-ntfy/`, directory mode 0700 on POSIX; on Windows the
  daemon tries to restrict the directory's ACL to the current user — on Python < 3.13 via `icacls`, best
  effort, a failure is only logged):

  | file | when | content |
  |---|---|---|
  | `daemon.sock` | unix transport | the listening socket; removed on exit |
  | `daemon.port` | tcp transport | two lines: the loopback port and a random token every request must carry; removed on exit |
  | `daemon.pid` | always | the daemon's pid; removed on exit |
  | `daemon.log` | always | never contains topic names, question text, or replies; written in Chinese by design |
  | `leases.json` | always | slot states and leases; slot numbers only, never topic names |
  | `topics.json` | `AGENT_NTFY_STORE=file` | the topic pool, plain JSON, mode 0600 |
  | `topics.dpapi` | `AGENT_NTFY_STORE=dpapi` | the topic pool encrypted with Windows DPAPI for the current user on this machine |

- Unix socket paths have a length limit that depends on the system. A deep `AGENT_NTFY_HOME` fails with
  `cannot listen for IPC: <path>: … Unix socket paths have a length limit (system-dependent); pick a shorter AGENT_NTFY_HOME`;
  the tcp transport has no such limit.
- Leftovers from a crash are handled at start: an endpoint file that nobody answers on is removed and
  replaced. If something *does* answer, the start is refused as "already running" — with tcp that can be
  an unrelated process that happens to sit on the port written in `daemon.port`; delete `daemon.port` by
  hand and start again.
- The daemon does not read `http_proxy` / `https_proxy` or the system proxy; only a proxy transparent to the process (a TUN-style VPN) applies.

## 4. Slots and leases

The pool is 5 topics by default (random names; `add-slot` grows it, there is no upper limit). Topic
names are the only credential and are never printed except by `confirm-sub` in the user's own terminal.
Where the pool is kept depends on `AGENT_NTFY_STORE` (§7): the macOS keychain, a 0600 file, or a
DPAPI-encrypted file.

The **lease holder** is the project: `proj:<git toplevel>` (the cwd when not in a git repository; a
worktree or a submodule is its own project), or the value of `AGENT_NTFY_TARGET` when that is set. Every
pane, session and context reset inside the same project shares that one lease. **One project holds one
slot, and one question at a time** (a second `ask` from the same project exits 4, busy; `notify` is not
subject to this and may run while a question is pending).

Separately from the holder, the lease records a **pane**: the herdr pane the project's most recent
command ran from. `ask`, `notify`, `slots`, `release` without an argument, `away on` and `away status`
all carry the project identity and refresh it (`confirm-sub` and `release <slot>` do not). Messages from
the phone are injected into that pane (§6). Outside herdr there is no pane, and such messages end in a
receipt on the phone.

| slot state | meaning |
|---|---|
| unassigned | no lease |
| leased, idle | leased to a project, no question waiting |
| leased, active | a question is waiting; cannot be released or taken over |
| confirming | a `confirm-sub` is running on it; treated as active |

`ask` and `notify` take a free slot automatically: any free slot when the project's remote mode is off,
**only a confirmed one when it is on** (`away: true` in the state file). Leases have **no TTL and are
never reclaimed**; release yours when your task ends (`agent-ntfy release` with no argument releases the
slot leased by the current project). Once no usable slot is free, `ask` exits 4 with the idle candidates
and their confirmation state (see [failures.md](failures.md) §5); that is the normal steady state, not an
error to hide.

```
agent-ntfy slots              # one line per slot: name, state, confirmed / unconfirmed, then "<holder>  since <time>" and "pane <id>" when leased
agent-ntfy release [<slot>]   # rc 0 "released slotN"; refuses an active slot (rc 3)
agent-ntfy add-slot           # rc 0 "added slot6 (not yet confirmed to reach the phone). Next: run  agent-ntfy confirm-sub slot6  in your own terminal"
```

After upgrading from a version whose lease holder was the pane id: `slots` shows those old leases with a
pane id (`wD:p1`) as the holder instead of `proj:…`; they are not migrated. `release <slot>` frees them, and
the next `ask` from the project leases a slot under the new identity. Restart the daemon after an upgrade:
an old daemon ignores the fields new clients send, and the new client's `--stop` is not understood by it
(stop the old one with its own CLI, or with SIGTERM).

## 5. The reachability gate (`confirm-sub`)

ntfy pushes only to subscribed devices, and a phone can be subscribed yet show no notification
(permission not granted): the message is on the server and in the app, nothing pops up, and no error
is raised anywhere. So before a slot is used for the first time, the human must **tap a button on a
test notification** for that slot. The result is stored; the slot is not asked again.

Default form, run by the user in their own terminal (stdout must be a TTY):

```
agent-ntfy confirm-sub slot1
topic for slot1: <topic name>
subscribe URL: https://ntfy.sh/<topic name>
Subscribe to the topic above in the ntfy app on your phone. Once subscribed, press Enter and I'll send a test notification with a button — when it pops up, tap the button and the check is done.
Press Enter once subscribed…
agent-ntfy: test notification sent — tap “Got it” in the phone's notification shade (within 600 s)…
✅ slot1 confirmed: the phone gets notifications; the agent can use it for questions from now on
```

- When **you** run it (stdout captured, no TTY) **inside herdr**, it opens a new pane, starts the default
  form there, prints `Started the reachability check for slot1 in herdr pane <id>. Tell the user: 1. … 2. … 3. …`
  and exits 0 at once. The topic name appears only in that pane. Relay the three steps; later, `slots` or
  `away status` shows whether the slot got confirmed. An already confirmed slot answers
  `slot1 is already confirmed … Add --again to confirm anew` (rc 0) without opening anything; an unknown
  slot exits 1.
- Without a TTY **outside herdr** it exits 4 without contacting the daemon, so the topic name cannot leak into your transcript.
- `--subscribed`: skip the topic display, send the test notification at once. Usable from your session when the user says they already subscribed; the tap on the phone is still required.
- `--show-topic`: print the topic name only, no test message; use only when the user asks for it (it lands in your output).
- `--again`: re-run on an already confirmed slot (new phone). `--timeout <seconds>`: wait for the tap (default 600).
- Only the button counts. A typed reply during confirmation is logged and answered with a `note:` line; it does not confirm.
- The panes that `confirm-sub` and `away on` open run the command as `python … --lang <lang> --home … <subcommand>`,
  where `<lang>` is the language resolved in *your* process (`--lang`, else `AGENT_NTFY_LANG`, else the system
  locale, else `en`); `daemon --detach` passes it the same way. The pane's own shell environment does not decide
  the wording there. Only a daemon you start by hand in a pane inherits that pane's shell environment.
- A pane opened by `away on` / a non-TTY `confirm-sub` runs `confirm-sub … --close-pane --report-to <opener pane>`:
  when the check ends (confirmed / timed out / interrupted / failed) it injects one line into the agent in the
  pane that opened it — `herdr agent prompt <pane> "[agent-ntfy] slotN is confirmed …"` (the `[agent-ntfy] `
  prefix marks a system event, as opposed to `[agent-ntfy remote] ` for the user's phone messages; a kimi
  target is woken the same way as for phone messages) — then, after a success, asks `Close this pane? [Y/n]`
  and closes itself (`herdr pane close`) on Enter or `y`; after a failure or timeout it stays open so the
  reason can be read. If the injection fails it prints one stderr line and keeps the exit code. Closing the
  pane by hand *before* pressing Enter and tapping the button cancels the check (the daemon sees the
  connection drop).
- Run by hand in a terminal (no `--report-to`), a successful `confirm-sub` ends with `You can close this
  terminal window now. Back in your agent's session, send it this line: agent-ntfy: slotN is confirmed; …` —
  the only way the result reaches an agent without herdr.

## 6. Messages from the phone when no question is pending

The daemon looks up the slot's lease and injects the text into the recorded pane as one line,
`[agent-ntfy remote] <text as typed>` (`herdr agent prompt <pane id>`; for a kimi target it also sends
`ctrl+s` to wake it). The prefix is protocol and never translated; the text after it is the human's,
unchanged. The daemon does not check whether the target is busy; your CLI queues input on its own. It
does check that the pane still exists.

If there is no lease, the lease has no pane (its commands were never run from inside herdr), the pane is
gone, or herdr is not available, the human gets a receipt on the phone (`[slotN] Message not delivered`)
with `Release slot` / `Ignore` buttons. Those buttons are handled by the daemon and never reach any agent.
You see none of this; if the human tells you they sent something you never received, `slots` and the
receipt on their phone are where to look.

## 7. Environment variables and files

Same variables as README §9 (plus `AGENT_NTFY_OFFLINE`), kept here so the agent need not open the README.

| variable | default | effect |
|---|---|---|
| `AGENT_NTFY_HOME` | `~/.agent-ntfy` | state directory (endpoint, pid, log, leases, and the pool file when a file store is used). With the unix transport keep it short: socket paths have a system-dependent length limit |
| `AGENT_NTFY_LANG` | (system locale, else `en`) | language of the fixed wording for everything without an `ask` context, and the fallback when `ask` / `notify` give no `lang`. Resolution order: `--lang` > this variable > system locale (`LC_ALL` / `LC_MESSAGES` / `LANG` starting with `zh`, or a Chinese Windows locale ⇒ `zh`) > `en`. `zh` / `en` only; any other value exits 1 |
| `AGENT_NTFY_TARGET` | – | overrides the lease holder (normally `proj:<project root>`); the same value reuses the same slot |
| `AGENT_NTFY_URL` | `https://ntfy.sh` | another ntfy instance (self-hosted). ntfy.sh's free tier allows about 250 messages per day per source IP, shared by all of your questions, notifications, updates and receipts |
| `AGENT_NTFY_IPC` | `unix` on POSIX, `tcp` on Windows | how clients reach the daemon: `unix` (socket file `daemon.sock`) or `tcp` (loopback port + token in `daemon.port`). Read when the daemon starts and by every client; `unix` on Windows and any other value exit 1 (`AGENT_NTFY_IPC=… is not a valid choice (only unix / tcp)`) |
| `AGENT_NTFY_STORE` | `keychain` on macOS, `dpapi` on Windows, `file` elsewhere | where the topic pool lives (read by the daemon only): `keychain` = macOS keychain item, authorised per application; `file` = `<home>/topics.json`, mode 0600; `dpapi` = `<home>/topics.dpapi`, encrypted for the current Windows user on this machine. `file` and `dpapi` are readable by every process of the same user account — wider than the keychain. Any other value exits with `AGENT_NTFY_STORE=… is not a valid choice (only keychain / file / dpapi)` |
| `AGENT_NTFY_KEYCHAIN` | `AGENT_NTFY_TOPICS` | keychain service name holding the topic pool (`keychain` store only) |
| `AGENT_NTFY_TOPIC_PREFIX` | `agent-ntfy` | prefix of newly generated topic names (`<prefix>-<20 random chars>`); letters, digits, `-`, `_`, max 40 |
| `AGENT_NTFY_OFFLINE` | – | **test suite only**: `1` skips the tests that talk to the real ntfy.sh. Nothing in `scripts/` reads it |
| `HERDR_ENV`, `HERDR_PANE_ID` | set by herdr | detected, not configured: inside herdr the pane id is recorded on the lease as the injection target |

`--home <dir>` on the command line overrides `AGENT_NTFY_HOME` and must come before the subcommand.
The `[tag]` in card titles is the project directory's name (cut to 41 bytes), not the pane id.

### The per-project state file: `<project root>/.agent-ntfy/state.json`

Written by the CLI, read by the agent (and by whatever rule the user keeps about remote mode). Project
root is the git toplevel, or the cwd when not in a git repository; a worktree or a submodule is its own
root and gets its own file. The directory carries its own
`.gitignore` (`*`), so git never sees it and the project's own `.gitignore` is not touched.

| field | written by | meaning |
|---|---|---|
| `away` | `away on` / `away off` | the human is away and wants decisions on the phone; while `true`, `ask` and `notify` use confirmed slots only |
| `slot` | `ask` / `notify` (on `sent` or on the unconfirmed-slot error), `release` (sets `null`), `away status` (corrected from the daemon) | the slot this project currently leases |
| `confirmed` | the same commands, plus `confirm-sub` (sets `true` when the confirmed slot is the recorded one) | whether that slot has passed `confirm-sub` |
| `target` | `away on`, `away off`, `ask`, `notify` | the lease holder identity (`proj:<root>`, or `AGENT_NTFY_TARGET`); informational, `release` leaves it |
| `updated` | every write | local time, ISO 8601 |

`away on` creates the directory; the other commands only update the file when the directory already
exists, so projects that never enabled remote mode get no directory. `release <slot>` and
`confirm-sub <slot>` touch the file only when that slot is the one recorded there. `away status` asks the
daemon for its leases and rewrites `slot` / `confirmed` when the file disagrees, printing
`state file corrected from the daemon's leases`; with no daemon it prints the file as is and
`daemon not running; not verified …`. Writes are atomic but unlocked: two simultaneous writers are
last-writer-wins. `away status` prints it in words, `away status --json` verbatim. The topic name is never
written here.

## 8. Language of the fixed wording

Resolution, highest first: the `lang` field of the `ask` / `notify` JSON (that card and all its later
updates) → `--lang` on the command line → `AGENT_NTFY_LANG` → the system locale (`LC_ALL` / `LC_MESSAGES` /
`LANG` starting with `zh`, or a Chinese Windows locale, gives `zh`) → `en`. Everything below the JSON field
(receipts, confirmation messages, CLI output, validation reports, `--help`) is resolved once when the process
starts; panes and daemons the CLI starts for you receive that value via `--lang`. A card keeps the language it was sent in even if the environment changes later. Control markers,
the `[tag]` prefix and the `[agent-ntfy remote] ` / `[agent-ntfy] ` injection prefixes are protocol, not wording, and never
change.
