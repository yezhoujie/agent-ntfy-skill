# The daemon, slots, and environment

`agent-ntfy` below means `python3 <skill dir>/scripts/agent_ntfy.py`.

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

Everything else (`ask`, `slots`, `release`, `confirm-sub`, `add-slot`) is a thin client that talks to
the daemon over a Unix socket and holds nothing. `ask` keeps its socket open until the answer arrives;
if the daemon dies, the socket drops and `ask` exits 3 at once. No silent hang.

## 2. Starting it (and how not to)

`ask` exits 3 and prints the start commands when the daemon is not running. You may start it for the
user. The only rule: **it must outlive you.**

| where you are | do this |
|---|---|
| inside herdr | open a pane and run the daemon there, so it is visible and herdr owns its lifetime: `herdr pane split --current --direction right --cwd "$PWD" --no-focus` returns the new pane id (`.result.pane`); then `herdr pane run <pane id> "python3 <skill dir>/scripts/agent_ntfy.py daemon"` |
| anywhere else | `agent-ntfy daemon --detach`: forks into its own session, prints `daemon: started in the background, pid N (log <home>/daemon.log)` once the socket answers |

**Never** start it as a background job of your own shell, under a Monitor, in a subagent, or with `&`
in a tool call: those die with your session, and every message the human sends afterwards is lost
without any error on their side.

A second start is refused (`a daemon is already running`); check with `--status` first. `--detach` may
report `not ready within 5 s, still starting (keychain prompt?)`: on first run macOS may ask the user to
allow keychain access, which nobody can answer from your session. Tell the user to look at the screen.

## 3. Status, stop, lifecycle

```
agent-ntfy daemon --status    # rc 0 + one line:  daemon: pid N  subscription: connected  pending questions: 0  confirming: 0  slots: 5
                              # rc 1: "daemon: not running", or pid alive but the socket does not answer
agent-ntfy daemon --stop      # SIGTERM, waits up to 30 s; rc 0 "daemon: pid N stopped"
```

- Subscription states: `connected` / `connecting` (just started) / `down for N s` (reconnecting with backoff 1 → 30 s). After 3 consecutive failures or 60 s down, every waiting `ask` gets a `note:` line on stderr; another when it is back.
- **Cold start does not replay.** Only messages arriving after the connection is up are seen; a question does not survive a daemon restart (its `ask` already exited 3).
- **Stopping** ends every waiting `ask` with rc 3 (`the daemon is stopping; the question went out …`). The cards on the phone are left as they are: the human may still answer, and that answer will be injected after the restart.
- Files under `AGENT_NTFY_HOME` (default `~/.agent-ntfy/`, mode 0700): `daemon.sock`, `daemon.pid`, `daemon.log`, `leases.json`. The log never contains topic names, question text, or replies; it is written in Chinese by design.
- The socket path has a hard length limit (104 bytes on macOS). A deep `AGENT_NTFY_HOME` fails with `can't bind the socket … pick a shorter AGENT_NTFY_HOME`, and clients see `AF_UNIX path too long`.
- The daemon does not read `http_proxy` / `https_proxy` or the system proxy; only a proxy transparent to the process (a TUN-style VPN) applies.

## 4. Slots and leases

The pool is 5 topics by default (random names, stored in the macOS keychain; `add-slot` grows it,
there is no upper limit). Topic names are the only credential and are never printed except by
`confirm-sub` in the user's own terminal.

A **target** is who is asking: your pane id inside herdr (`$HERDR_PANE_ID`, taken automatically and not
overridable); otherwise `AGENT_NTFY_TARGET` if set, else `host:<hostname>|sid:<session id>`. **One
target holds one slot, and one question at a time** (a second `ask` from the same target exits 4, busy).

| slot state | meaning |
|---|---|
| unassigned | no lease |
| leased, idle | leased to a target, no question waiting |
| leased, active | a question is waiting; cannot be released or taken over |
| confirming | a `confirm-sub` is running on it; treated as active |

`ask` takes a free slot automatically. Leases have **no TTL and are never reclaimed**; release yours
when your task ends (`agent-ntfy release` with no argument releases the slot leased by the current
target). Once every slot is leased, `ask` exits 4 with the idle candidates and their confirmation state
(see [failures.md](failures.md) §5); that is the normal steady state, not an error to hide.

```
agent-ntfy slots              # one line per slot: name, state, confirmed / unconfirmed, lease holder and since when
agent-ntfy release [<slot>]   # rc 0 "released slotN"; refuses an active slot (rc 3)
agent-ntfy add-slot           # rc 0 "added slot6 (not yet confirmed to reach the phone). Next: run  agent-ntfy confirm-sub slot6  in your own terminal"
```

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

- Without a TTY it exits 4 without contacting the daemon, so the topic name cannot leak into your transcript.
- `--subscribed`: skip the topic display, send the test notification at once. Usable from your session when the user says they already subscribed; the tap on the phone is still required.
- `--show-topic`: print the topic name only, no test message; use only when the user asks for it (it lands in your output).
- `--again`: re-run on an already confirmed slot (new phone). `--timeout <seconds>`: wait for the tap (default 600).
- Only the button counts. A typed reply during confirmation is logged and answered with a `note:` line; it does not confirm.

## 6. Messages from the phone when no question is pending

The daemon looks up the slot's lease and injects the text into that target as a plain instruction
(`herdr agent prompt <pane id>`; for a kimi target it also sends `ctrl+s` to wake it). It does not check
whether the target is busy; your CLI queues input on its own. It does check that the target still exists.

If there is no lease, the pane is gone, or herdr is not available, the human gets a receipt on the phone
(`[slotN] Message not delivered`) with `Release slot` / `Ignore` buttons. Those buttons are handled by the
daemon and never reach any agent. You see none of this; if the human tells you they sent something you
never received, `slots` and the receipt on their phone are where to look.

## 7. Environment variables and files

Same table as README §9, kept here so the agent need not open the README.

| variable | default | effect |
|---|---|---|
| `AGENT_NTFY_HOME` | `~/.agent-ntfy` | state directory (socket, pid, log, leases). Keep it short: Unix socket paths max 104 bytes on macOS |
| `AGENT_NTFY_LANG` | `en` | language of the fixed wording for everything without an `ask` context, and the fallback when `ask` gives no `lang`. `zh` / `en` only; any other value exits 1 |
| `AGENT_NTFY_TARGET` | – | outside herdr: the identity that holds the lease; the same value reuses the same slot. Ignored inside herdr |
| `AGENT_NTFY_URL` | `https://ntfy.sh` | another ntfy instance (self-hosted). ntfy.sh's free tier allows about 250 messages per day per source IP, shared by all of your questions, updates and receipts |
| `AGENT_NTFY_KEYCHAIN` | `AGENT_NTFY_TOPICS` | keychain service name holding the topic pool (read by the daemon only) |
| `AGENT_NTFY_TOPIC_PREFIX` | `agent-ntfy` | prefix of newly generated topic names (`<prefix>-<20 random chars>`); letters, digits, `-`, `_`, max 40 |
| `HERDR_ENV`, `HERDR_PANE_ID` | set by herdr | detected, not configured: inside herdr the pane id is the target and the `[tag]` in titles |

`--home <dir>` on the command line overrides `AGENT_NTFY_HOME` and must come before the subcommand.

## 8. Language of the fixed wording

Resolution, highest first: the `lang` field of the `ask` JSON (that card and all its later updates) →
`AGENT_NTFY_LANG` (receipts, confirmation messages, CLI output, validation reports, `--help`) → `en`.
A card keeps the language it was sent in even if the environment changes later. Control markers and the
`[tag]` prefix are protocol, not wording, and never change.
