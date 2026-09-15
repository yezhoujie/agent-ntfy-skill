# Exit codes, stderr, and what to tell the user

Every failure is loud: a non-zero exit plus a stderr line, prefixed `agent-lark: `, that says what
happened — and for `ask`, whether the question is on the phone. Never treat an empty stdout as "no
answer yet": read `rc` first.

`agent-lark` in the quoted text is the CLI's own name for itself; on disk it is `dist/cli.mjs`.

## Contents

1. `ask`, `notify` and `send-file` exit codes at a glance
2. rc 1: validation report and other rejected input
3. rc 2: timeout
4. rc 3: channel failure
5. rc 4: a human must act
6. rc 130: interrupted
7. `note:` lines while waiting
8. Other subcommands (`setup` / `daemon` / `bind` / `unbind` / `rename` / `away` / `status`)
9. Feishu-specific: urgent flag, renaming, group creation, voice notes, "Read 0/0"

## 1. `ask`, `notify` and `send-file` exit codes at a glance

| rc | meaning | on the phone? | stdout |
|---|---|---|---|
| 0 | `ask`: reply received · `notify`: card sent · `send-file`: file sent | yes | `ask`: the reply, verbatim, one trailing newline · `notify`: `Notification sent (a reply from the phone is injected into this pane as an instruction)` · `send-file`: `Sent to the project group` |
| 1 | input rejected (validation, bad JSON, bad argument, file refused) | **no** | empty |
| 2 | `ask` only: no reply within the timeout | yes | empty |
| 3 | channel failure: daemon not running / not connected to Feishu / send failed / daemon stopping / connection lost | stderr says which | empty |
| 4 | needs a human: project not bound · a question already pending · (`away on`) no credentials or earlier groups to choose from | no | empty |
| 130 | Ctrl-C on the `ask` client; the card is cancelled by the daemon | it was, now cancelled | empty |

Codes 2, 3, 4 and 130 are distinct on purpose: the right next step differs for each. `notify` and
`send-file` never return 2 (nothing is waited for) and treat every other code as `ask` does.

## 2. rc 1: validation report and other rejected input

All problems of a question are reported in one run; fix them all before calling again. Nothing was sent.

```
agent-lark: This question card has 7 problem(s); nothing was sent:
  description: required (background for someone who has seen none of the work)
  blocker: required (exactly what is blocked)
  reasoning: required (your leaning plus the strongest objection)
  question: required (a question answerable in one sentence)
  options: at least 2 items (one option is not a choice)
  recommend: "temp" is not the id of any option
  lang: must be "zh" or "en", got "fr"
```

Other lines you may see, and the fix:

| line | fix |
|---|---|
| `title: required and non-empty` · `title: must be a single line` · `title: over 200 characters, got N` | give a one-line title |
| `doing: over 4000 characters, got N` (same for `description` / `blocker` / `reasoning` / `question`) | shorten; the reader is on a phone anyway |
| `options: must be an array` · `options: at most 5 items (more means the question has not converged)` | 2 to 5 objects |
| `options[1].id: required` · `options[2].id: "keep" is duplicated` · `options[1].label: required (the button text, and what comes back when it is tapped)` · `options[1].label: over 60 characters` · `options[1].consequence: required (what actually happens if chosen, including the cost)` · `options[1].consequence: over 500 characters` | every option needs non-empty `id`, `label`, `consequence`; ids unique |
| `recommend: required; the id of one option` · `recommend: "x" is not the id of any option` | point `recommend` at an existing id |
| `recommend: "wipe" is marked danger; an irreversible or high-cost option cannot be the recommendation — list it and let the human choose` | recommend something else, or drop `danger` if it was wrong |
| `recommend: with select "single" (the default) it must be one option id, a string — set select: "multi" to recommend several` · `recommend: with select "multi" it must be an array of option ids (strings)` · `recommend: with select "multi" the array must be non-empty — at least one option to tick by default` · `recommend: "keep" is listed twice` | match `recommend`'s shape to `select` |
| `select: must be "single" or "multi", got "x"` | one of the two, or omit it |
| `agent-lark: stdin is not valid JSON: <parser message>` | the heredoc is not valid JSON; check quotes and commas |
| `agent-lark: This command reads one JSON object from stdin. Feed it with a heredoc.` | stdin was a terminal; pipe the JSON in |
| `agent-lark: --timeout must be a positive integer (seconds)` | a number of seconds greater than 0 |

The same report for `notify` starts `agent-lark: This notification has N problem(s); nothing was sent:`
and knows `title` (as above), `body: required and non-empty`, `body: over 8000 characters` and `lang`.

Other rc 1 cases, one line each: `Unknown command "x". See agent-lark --help.` · `--home needs a directory`
· `Usage: agent-lark send-file <path> [--caption <text>]` · `Usage: agent-lark rename "<task name>"` ·
`Usage: agent-lark away on [--name <task>] [--reuse <chat_id> | --new] | off | status [--json]` ·
`task name: over 60 characters (code points), got N` · `--reuse and --new cannot be combined` ·
`--reuse oc_…: not one of the groups this project could take back` · `group oc_… is the live group of another project (<root>); unbind it there first` ·
`this project is not bound` (`unbind`) · `--store must be keychain / file / none` (`setup`).

`send-file` rc 1: `file not found: <path>` · `not a regular file: <real path>` · `file too large: 12.3 MB, limit 10 MB`
(images: `png jpg jpeg gif webp bmp` up to 10 MB; anything else 30 MB) · and, for a path outside the allowlist:

```
agent-lark: refusing to send /etc/hosts
Only files under these directories can be sent:
  this project /Users/me/work/my-project
  /Users/me/.agent-lark/media
  /var/folders/…/T
(this keeps send-file from reading arbitrary files off the machine)
```

## 3. rc 2: timeout

```
agent-lark: no answer after 43200 s
```

The card on the phone is rewritten to `⌛ … · Timed out` (grey header, buttons gone). If the human
answers later by typing in the group, that message is injected into your session as an instruction (see
SKILL.md, "Messages the human sends on their own"), so do not ask the same question again just to catch
it. Decide, or ask again with a different question, according to your own policy.

**Killed by your own harness is not a timeout.** `ask` holds a connection to the daemon for as long as
it waits. If the tool you run it through has a shorter limit than `--timeout` and kills the process, the
connection drops, the daemon treats the question as cancelled (card rewritten to `⚠️ … · Cancelled`,
buttons removed), and you receive neither stdout nor an exit code — only the harness's own "timed out"
message. The human sees a cancelled card and may not answer at all. Two safe patterns:

- `--timeout` no larger than the harness limit (e.g. `--timeout 540` under a 600 s cap) and handle rc 2
  as the expected path; ask again later if you still need the answer.
- Run `ask` as a background job of your own session (whatever your harness offers for that), with stdout
  and stderr redirected to files, and read the files plus the exit status when it finishes. The daemon
  must still be a separate process ([daemon.md](daemon.md) §2); only the client may live in your session.

## 4. rc 3: channel failure

**Daemon not running** (not sent):

```
agent-lark: daemon is not running. Start it first: agent-lark daemon --detach
```

Start it as described in [daemon.md](daemon.md) §2, then call again. A different connect error reads
`cannot connect to the daemon: <message>`.

**Daemon up, Feishu not reached yet** (not sent):

```
agent-lark: not connected to Feishu (<reason>); the daemon keeps retrying, try again shortly
```

The daemon opens its endpoint before the Feishu handshake and retries the handshake with backoff (5 s
doubling to 60 s). `<reason>` is the last handshake error (`still connecting` right after start, or
`the connection to Feishu dropped, reconnecting` after a drop). `daemon --status` shows the same as
`connected false` + `last error:`. Wait and retry; if it never connects, the reason (credentials,
network) is for the user.

**Send failed** (not sent): `agent-lark: send failed: <SDK error>` — Feishu refused or the request
failed after the connection was up. Report it to the user; retrying immediately rarely helps.

**Daemon stopped while you were waiting** (sent, then cancelled): `agent-lark: the daemon is stopping; the question was sent but no answer will arrive this time`
— someone ran `daemon --stop --force` or the daemon got a signal. The card is rewritten to
`⚠️ … · Cancelled`. Ask again once it is back.

**Connection lost** (`ask`: sent; the reply can no longer reach this call): `agent-lark: the daemon dropped the connection before answering (it may have crashed or been stopped)`
— check `daemon --status`, restart if needed, ask again. A late typed reply is injected as an
instruction, as after a timeout.

**Daemon accepted the connection but never answered**: `agent-lark: the daemon did not respond in time`
— only the commands with a client-side limit give this line (`daemon --status` / `status` / `away on`'s
pings, 2–5 s); `ask` and `notify` wait without a limit. Check `daemon --status`; restart it if that
hangs too.

**`away on` could not get a connection** (rc 3, remote mode not enabled, nothing written):
`agent-lark: daemon is up but not connected to Feishu: <reason>` after 15 s of waiting, or the `--detach`
failure line (`daemon started but did not answer within 10 s; see the log: <home>/daemon.log`).

**`rename` refused by Feishu** (rc 3): `agent-lark: renaming the group failed: Feishu error <code> <msg>` (§9).

**Anything unexpected** ends with rc 3 and the error's stack on stderr; relay it.

## 5. rc 4: a human must act

The channel never asks the human anything itself (stdout is captured, so a prompt would hang). It exits
4 and you relay the situation to the user **in your own conversation**, then retry once they have acted.

**Project not bound** (`ask` / `notify` / `send-file`; not sent):

```
agent-lark: this project is not bound yet; run agent-lark away on first
```

Run `away on` in the project (SKILL.md, "Remote mode"); it may itself exit 4 with one of the next two.

**No credentials** (`away on`): `agent-lark: No Feishu app credentials yet. Run once: agent-lark setup`
— ask the user whether to scan a QR code for a new app (`setup`) or reuse an existing app id
(`setup --app-id cli_xxxxxxxx` with the secret in `AGENT_LARK_APP_SECRET` or the env file); `setup` is
theirs to run.

**Earlier groups to choose from** (`away on` / `bind` without `--reuse` / `--new`):

```
agent-lark: this project has no live group, but 2 earlier group(s) could be taken back (renamed) instead of creating another:
old task [proj]  released 2026-01-02T03:04:05.000Z  oc_xxxxxxxx
(unnamed)  released -  oc_yyyyyyyy
ask the user which to reuse (rename) or create new; rerun with --reuse <chatId> or --new
```

One line per candidate: name, when it was released (`-` for a group found in Feishu with no local
record), group id. Relay the list; the human decides; rerun with their choice. This is the normal path
for a project that had a group before.

**A question is already pending** (`ask`): `agent-lark: this project already has a question pending on the phone; one at a time`
— wait for it (or for its timeout). `notify` and `send-file` may still be sent.

**`unbind` / switching groups while a question is pending**: `agent-lark: a question is still pending on the phone; answer it or wait for the timeout`.

**`daemon --stop` while questions are pending**: see [daemon.md](daemon.md) §3 (`--force` overrides).

**`rename` with no live group**: `agent-lark: this project has no live group; run agent-lark away on first`.

**Creating a group is impossible**: `agent-lark: nobody to invite into a new group (the app owner is not recorded). Use --chat <chat_id> to bind a group you created yourself.`
(credentials came from the environment without `AGENT_LARK_OWNER_OPEN_ID`, or an old `setup`) — or
`creating the group failed: <error>` with `If this is a permission problem the app lacks the im:chat (create group) scope: run agent-lark setup --update, or bind an existing group with --chat <chat_id>.`
when Feishu refused for a permission reason (any other reason is rc 3 with the same text).

**`setup --app-id` without a secret**: `App Secret not found. Do not put it on the command line (argv is visible machine-wide); use either:`
with the two ways to supply it (rc 4). **QR code expired** before it was scanned:
`The QR code expired before it was scanned. Run again: agent-lark setup (original error: …)` (rc 4) — the user reruns `setup`
(every `setup` line is printed in Chinese and English side by side).

## 6. rc 130: interrupted

Ctrl-C on the `ask` client is Node's default handling: exit 130, nothing on stderr. The daemon sees the
connection drop and rewrites the card to `⚠️ … · Cancelled` (buttons gone). Only the client was
interrupted; the daemon is unaffected — **do not restart it**. Simply call `ask` again; a message the
human types under the cancelled card arrives as an instruction.

## 7. `note:` lines while waiting

Lines starting `note: ` on stderr while a command blocks are informational; the call keeps going:

- `note: sent to the Feishu group, waiting for the answer (up to 43200 s)` — every `ask`, once the card is on the phone.
- `note: the urgent flag was not delivered (<why>); the question itself was sent and is waiting as usual` — `--urgent` only (§9).
- `bind` / `away on`: `note: this project already has a live group; --reuse / --new were ignored (unbind first to pick another group)` ·
  `note: bound, but renaming the group failed: <error>` · `note: not connected to Feishu; the group's name and description were left as they are` ·
  `note: could not look through the Feishu groups for earlier ones of this project (<error>); only local records were considered`.

## 8. Other subcommands

| command | 0 | 1 | 3 | 4 |
|---|---|---|---|---|
| `setup` (QR) | app registered, credentials saved (`✅ …`), next steps printed; or `Credentials already exist (from <origin>). Add --update … --reset …` when some exist and neither `--update` nor `--reset` nor `--app-id` was given | `--store` not one of `keychain / file / none` | the scan-code registration failed (after up to 3 retries on network errors) | the QR code expired before it was scanned |
| `setup --app-id cli_…` | credentials checked against Feishu once, saved | – | the pair does not work (`These credentials cannot reach Feishu: …`) | no secret in `AGENT_LARK_APP_SECRET` / the env file |
| `daemon --detach` | started (pid printed), or `daemon is already running` | – | did not answer within 10 s | – |
| `daemon` (foreground) | clean shutdown after a signal or `--stop` | – | already running | no credentials · `bindings.json` unreadable |
| `daemon --status` | two status lines | not running / no answer | – | – |
| `daemon --stop` | `daemon: stopped` · `daemon: was not running` | – | still answering 10 s after the request | questions pending (no `--force`) |
| `bind` | `✅ Created … / ✅ Took back … / ✅ Already bound … / ✅ Bound to existing group oc_…` | task name too long · `--reuse` + `--new` · `--reuse` not a candidate · `--chat` is another project's live group | not connected to Feishu · group creation failed | candidates to choose from · no owner recorded · creation refused for permissions · switching groups with a question pending |
| `unbind` | `Unbound. The Feishu group "…" stays in Feishu; the next away on in this directory offers to rename and reuse it.` | not bound | daemon not running | a question is pending |
| `rename "<task>"` | `Renamed the Feishu group to "<task> [<dir>]"` | empty or over-long name | not connected · Feishu refused (§9) | no live group |
| `away on` | remote mode enabled (daemon line, group line, `Remote mode is on: …`; outside herdr one more line) | task name too long · `--reuse` + `--new` · `--reuse` not a candidate | daemon did not come up · not connected within 15 s · group creation failed | no credentials · candidates to choose from · no owner · creation refused for permissions |
| `away off` | `Remote mode is off.` (also when nothing is bound) | – | daemon not running | – |
| `away status [--json]` | printed, whatever the state | – | – | – |
| `status` | printed: credentials and their layers, herdr, daemon (or `daemon: not running (agent-lark daemon --detach)`), bindings | – | – | – |
| `help` / no command | the command summary | unknown command | – | – |

`away on` stops at the first failure and leaves the switch off: a failed step never leaves remote mode
half-on.

## 9. Feishu-specific: urgent flag, renaming, group creation, voice notes, "Read 0/0"

- **`--urgent` never fails the question.** The card is sent first; the in-app urgent flag is a second
  call, and any refusal is only a `note:` (§7): `the app owner is not recorded, so there is nobody to flag; rerun agent-lark setup --update to record it`,
  or `Feishu error <code> <msg>` — typically the app lacks the `im:message.urgent` scope (`setup --update`
  asks for it). Flag only what deserves it: an irreversible action, a short timeout.
- **Renaming a group** goes through Feishu's chat update. The bot may rename groups it created freely; a
  group the human created only when its settings let every member edit group info. Codes 232002 /
  232016 (not allowed) and 232011 (the bot is not a member) get this appended to the rc 3 line:
  `The bot may only rename a group it owns, or one whose settings let every member edit group info (232002 / 232016), and must be a member of it (232011).`
  A refused rename during `bind` / `away on` is only a `note:` — the binding is made regardless.
- **Group creation** needs the `im:chat` scope and a recorded app owner to invite (§5). The group the
  bot creates is the bot's own (that is what lets it rename the group later); the app owner is invited
  into it as its first member.
- **Voice notes.** Transcription needs the `speech_to_text:speech` scope **and a paid Feishu tenant**:
  on the free (personal) plan Feishu answers HTTP 400 `code 99991400, msg "request trigger frequency limit"`
  even with the scope granted, and there is nothing to configure around it. The daemon injects one
  sentence with that code and message and both causes; the audio file is saved and listed (`[saved: …]`)
  either way. Tell the user to type it this time.
- **"Read 0/0" under your card** is Feishu's read-receipt mechanism for bot messages, not a delivery
  failure. The channel's own "received" marks are: the card header turns **green** (`✅ … · Answered`)
  when a question is answered — grey for timed out / cancelled — and a **`Get` reaction** appears on a
  message the human sent once it reached the terminal. A `notify` card (light blue) has no state and
  never changes, replying to it or not.
