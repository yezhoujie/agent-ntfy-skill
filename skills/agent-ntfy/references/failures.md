# Exit codes, stderr, and what to tell the user

Every failure is loud: a non-zero exit plus a stderr line that says what happened **and whether the
message was sent**. Never treat an empty stdout as "no answer yet": read `rc` first.

`agent-ntfy` in the quoted stderr text is the CLI's own name for itself; on disk it is `scripts/agent_ntfy.py`.

## Contents

1. `ask` and `notify` exit codes at a glance
2. rc 1: validation report
3. rc 2: timeout
4. rc 3: channel failure
5. rc 4: a human must act on the terminal side
6. rc 130: interrupted
7. Warnings while waiting
8. Other subcommands (`confirm-sub` / `release` / `add-slot` / `slots` / `away` / `daemon`)
9. Storage and transport errors (daemon side)

## 1. `ask` and `notify` exit codes at a glance

| rc | meaning | message sent? | stdout |
|---|---|---|---|
| 0 | `ask`: reply received · `notify`: notification sent | yes | `ask`: the reply, verbatim, one trailing newline · `notify`: `notification sent on slotN (if the user replies, it arrives as an instruction)` |
| 1 | input validation failed | **no** | empty |
| 2 | `ask` only: no reply within the timeout | yes | empty |
| 3 | channel failure: daemon not running / connection lost / publish failed | stderr says which | empty |
| 4 | needs a human on the terminal side: unconfirmed slot / no usable slot / this project already waiting | no | empty |
| 130 | Ctrl-C (handled, with the sent / not-sent note, for `ask` only) | stderr says which | empty |

Codes 2, 3, 4 and 130 are distinct on purpose: the right next step differs for each. `notify` never
returns 2 (it does not wait) and treats every other code as `ask` does.

## 2. rc 1: validation report

All problems are reported in one run; fix them all before calling again. Nothing was sent.

```
agent-ntfy ask: input validation failed (7 issue(s)); fix them all and retry. Message NOT sent.

  description: missing. Required: the background, written for someone who has not seen any of the work
  blocker    : missing. Required: exactly what is blocked
  options    : only 1 item(s); 2-5 required (a single option is not a choice)
  recommend  : "temp" is not one of the option ids (existing ids: keep)
  reasoning  : missing. Required: why you lean that way + the strongest objection
  question   : missing. Required: one question answerable in one sentence
  lang       : "fr" is not a valid choice (only zh / en); it selects the language of the fixed wording — omit it to fall back to AGENT_NTFY_LANG, then en
```

Other lines you may see, and the fix:

| line | fix |
|---|---|
| `body       : renders to N bytes, limit 3584, M bytes over. Trim description / consequence / reasoning (nothing is truncated for you)` | shorten those fields; CJK is 3 bytes per character |
| `title      : N bytes, limit 960, M bytes over …` / `contains a line break` | shorten the title; keep it on one line |
| `options    : item 2 lacks consequence. Each item needs id / label / consequence, all non-empty` / `duplicate id: keep (items 1, 3)` | every option needs non-empty `id`, `label`, `consequence`; ids unique |
| `options    : 6 items; 2-5 required …` | merge or drop options; more than 5 means the question has not converged |
| `JSON       : not valid JSON: Illegal trailing comma before end of object (line 1, column 14)` | the heredoc is not valid JSON; check quotes and commas |
| `agent-ntfy: AGENT_NTFY_LANG=xx is not a valid choice (only zh / en) …` (also rc 1, printed before any subcommand runs) | fix or unset the environment variable |
| `agent-ntfy: AGENT_NTFY_IPC='xx' is not a valid choice (only unix / tcp); set one of them or unset it (platform default)` (rc 1, before any subcommand runs; `unix` on Windows is rejected the same way) | fix or unset the environment variable |

The same report for `notify` starts with `agent-ntfy notify: input validation failed (N issue(s)); …`
and knows two fields: `title      : an empty string. Required: the one-line hook shown in the notification shade — the preview shows nothing else`,
`body       : an empty string. Required: the notification body; Markdown allowed (bold / lists / rules)`;
an over-long body reads `body       : renders to N bytes, limit 4096, M bytes over. Trim body (nothing is truncated for you)`.

The report is in the language selected by the JSON `lang` (if valid), else the process language (`--lang`, else `AGENT_NTFY_LANG`, else the system locale, else English).

## 3. rc 2: timeout

```
agent-ntfy: no reply within 43200 s (the message was sent; the user's later reply will be delivered as an instruction)
```

The card on the phone is replaced by `⌛ Timed out · …` without a button (with a `--timeout` under
roughly 90 s the card may keep its original look — observed: a 5 s timeout did not flip the card, a 90 s
one did; the exit code is unaffected). If the human answers later, that answer is injected into your
session as an instruction (see SKILL.md, "Messages the human sends on their own"), so do not ask the same
question again just to catch it. Decide, or ask again with a different question, according to your own
policy.

**Killed by your own harness is not a timeout.** `ask` holds a connection to the daemon for as long as it waits. If the tool you run it through has a shorter limit than `--timeout` and kills the process, the connection drops, the daemon treats the question as cancelled (card rewritten to `⚠️ Cancelled · …`, button removed, notification cleared), and you receive neither stdout nor an exit code — only the harness's own "timed out" message. A late answer from the human is then injected as an instruction, but the human sees a cancelled card and may not answer at all. Two safe patterns:

- `--timeout` no larger than the harness limit (e.g. `--timeout 540` under a 600 s cap) and handle rc 2 as the expected path; ask again later if you still need the answer.
- Run `ask` as a background job of your own session (whatever your harness offers for that), with stdout and stderr redirected to files, and read the files plus the exit status when it finishes. The daemon must still be a separate process (see [daemon.md](daemon.md) §2); only the client may live in your session.

## 4. rc 3: channel failure

Read the parenthesis: it tells you whether the message went out.

**Daemon not running** (not sent):

```
agent-ntfy: can't connect to the daemon (<home>/daemon.sock: No such file or directory). Message NOT sent.
The daemon is not running. Start it:
  inside herdr : open another pane and run  agent-ntfy daemon      (visible, herdr owns its lifetime)
  outside herdr: agent-ntfy daemon --detach                     (detached; manage with --status / --stop)
  ⚠️ don't start it from the agent's own shell or as its background task — it dies with the agent
```

With the tcp transport (the default on Windows) the path in the parenthesis is `<home>/daemon.port`.
Start the daemon as described in [daemon.md](daemon.md), then call again. (`AF_UNIX path too long` in
place of `No such file or directory` means `AGENT_NTFY_HOME` is too deep for a Unix socket; see daemon.md §3.)

**Publish failed** (not sent): `agent-ntfy: message NOT sent: publishing to ntfy failed: <reason>`. The reason is an HTTP status, a connection error, or `This is rate limiting, not a code error` (ntfy.sh allows about 250 messages per day per source IP). Report it to the user; retrying immediately rarely helps.

**Daemon stopped while you were waiting** (sent): `agent-ntfy: message sent, but the daemon is stopping; the question went out, the user's later reply will be delivered as an instruction`. Same handling as a timeout once the daemon is back.

**Connection lost** — `agent-ntfy: connection to the daemon lost (message sent; the reply can no longer reach this call)` or `… (message NOT sent)`; `agent-ntfy: talking to the daemon failed: <error> (message sent)` — restart or check the daemon (`daemon --status`), then follow the sent / not-sent hint.

**Daemon accepted the connection but never answered** (not sent): `agent-ntfy: talking to the daemon failed: no response from the daemon (message NOT sent)` — one-shot commands (`notify`, `slots`, `release`, `add-slot`, `daemon --status|--stop`) give up after 60 s (`REQUEST_TIMEOUT`, longer than the daemon's 30 s publish limit) instead of hanging; the daemon is stuck outside its main loop — check `daemon --status`, restart it if that hangs too. `ask` and `confirm-sub` are not subject to this limit (they wait for a human).

**Rejected by the daemon** (not sent): `agent-ntfy: message NOT sent: unauthenticated connection (token mismatch)` — tcp transport only: the token the client sent is not the running daemon's (`daemon.port` was rewritten after the client read it, or edited); run `daemon --status` and call again. A daemon that cannot read its own state answers `… NOT sent: <storage error>` (§9).

## 5. rc 4: a human must act on the terminal side

The channel never asks the human anything itself (stdout is captured, so a prompt would hang). It exits 4 and you relay the situation to the user **in your own conversation**, then retry once they have acted.

**Slot not confirmed** (first use of a slot on this machine, or a newly added one):

```
agent-ntfy: message NOT sent: Slot slot1 has not been confirmed to reach the phone yet. Ask the user to run agent-ntfy confirm-sub slot1 in their own terminal, subscribe and tap the button as prompted, then retry
```

Inside herdr you can do more than relay: run `confirm-sub slot1` yourself; it opens a pane for the user and tells you the three steps to relay (daemon.md §5). Outside herdr, tell the user: run `python3 <skill dir>/scripts/agent_ntfy.py confirm-sub slot1` in their own terminal (it prints the topic name, which must not pass through your output), subscribe in the ntfy app, press Enter, and tap the button on the test notification. If the user says they already subscribed that topic, you may run `confirm-sub slot1 --subscribed` yourself (no topic is printed); it still needs the tap on the phone. If the test notification never pops up, the phone's notification settings are the problem; the skill's README (for humans) has the checklist.

**All slots leased**:

```
agent-ntfy: message NOT sent: All slots are leased. Release an idle one (agent-ntfy release <slot>, then retry; candidates: slot2 (confirmed), slot3 (unconfirmed — picking it means one more round on the phone)) or add one (agent-ntfy add-slot, which then needs confirming)
agent-ntfy:   candidate: slot2 (confirmed)
agent-ntfy:   candidate: slot3 (unconfirmed — picking it means one more round on the phone)
```

This is the normal state once the pool has been in use for a while (leases never expire). Ask the user which idle slot to release, or whether to add one; a confirmed candidate saves them a round on the phone. Slots with a question in flight are never listed. Then run `release <slot>` or `add-slot` and retry.

**No confirmed slot while remote mode is on** (`away: true` in the project's state file): `agent-ntfy: message NOT sent: In remote mode only confirmed slots can be used, and every confirmed slot is taken. Release an idle confirmed one (agent-ntfy release <slot>, then retry; candidates: …) or wait for the user to confirm a free slot at their terminal (agent-ntfy confirm-sub <slot>)`, followed by the same `candidate:` lines. Free but unconfirmed slots are not taken automatically, because nobody is at the keyboard to confirm them; the human decides.

**Busy**: `agent-ntfy: message NOT sent: this target already has a question waiting on slot1; wait for it to finish` — another `ask` from the same project is still blocking (`notify` is not affected and may be sent meanwhile). `slot slot1 is in the middle of a reachability check; retry once it finishes` — a `confirm-sub` is running on that slot.

## 6. rc 130: interrupted

`agent-ntfy: interrupted (message sent; the user's later reply will be delivered as an instruction)` or `… (message NOT sent)`. Only the `ask` client was interrupted; the daemon is unaffected — **do not restart it** (130 is a separate code precisely so this is not mistaken for rc 3). If stderr says the message was sent, the card becomes `⚠️ Cancelled · …` and a late reply still reaches you as an instruction: handle it like rc 2. If it says NOT sent, simply call `ask` again.

## 7. Warnings while waiting

Lines starting `agent-ntfy: note:` on stderr while `ask` blocks are informational; the call keeps waiting:

- `the ntfy subscription is down (N consecutive failures, S s) and still reconnecting; replies arriving meanwhile will be replayed once it recovers` / `the ntfy subscription is back`
- `the ntfy subscription is currently down and reconnecting; the question went out, the reply will be replayed once it recovers`

## 8. Other subcommands

| command | 0 | 1 | 2 | 3 | 4 | 130 |
|---|---|---|---|---|---|---|
| `notify` | sent (stdout names the slot) | validation failed | – | daemon not running / publish failed / rejected by the daemon | unconfirmed slot / no usable slot | – |
| `confirm-sub <slot>` | confirmed (or already confirmed; inside herdr without a TTY: pane opened) | unknown slot | no tap (or no Enter) within the timeout, default 600 s | daemon not running / publish failed / state file error | not a terminal and no `--subscribed`, outside herdr or the pane could not be opened; stdin ended before Enter; slot busy | Ctrl-C |
| `release [<slot>]` | released | unknown slot | – | daemon not running; slot has a question waiting; slot has no lease; state file error | – | – |
| `add-slot` | added (prints the new slot; confirm it next) | – | – | daemon not running / pool store write failed | – | – |
| `slots` | listed | – | – | daemon not running | – | – |
| `away on` | remote mode enabled (stdout says whether a slot is ready, leased lazily, or being confirmed in a pane) | – | – | state directory not writable; daemon did not come up; could not open a herdr pane | outside herdr and the slot needs confirming; every slot is leased (`All slots are leased. …` + `candidate:` lines) | – |
| `away off` / `away status` | done / printed (also when remote mode was never enabled) | – | – | state file unreadable or unwritable | – | – |
| `daemon --status` | running, one status line on stdout | not running (`daemon: not running`), or `daemon: no answer (pid file N still there; it may have died)` | – | – | – | – |
| `daemon --stop` | `daemon: pid N stopped`; also when nothing was running and no pid file exists (`daemon: not running`) | stale pid file but no answer; `did not acknowledge the stop`; `did not exit within 30 s` | – | – | – | – |
| `daemon --detach` | started, pid printed | – | – | already running; child exited (`exit code N, see <log>`); not ready within 5 s | – | – |
| `daemon` (foreground) | clean shutdown | – | – | could not start: endpoint in use / cannot listen / storage error (§9), one line on stderr | – | – |

`confirm-sub --show-topic <slot>` prints the topic name and exits 0 without sending anything; the name lands in your output, so only use it when the user asked for that.

## 9. Storage and transport errors (daemon side)

These are reported by the daemon itself: on stderr of a foreground `daemon` (rc 3), in `<home>/daemon.log`
when it was detached (then `--detach` reports `the daemon did not come up (exit code 3), see <log>`), or as
`message NOT sent: …` to a client when they happen later (rc 3 on the client). None of them is something
the agent fixes alone; relay the line to the user.

| stderr / log line | meaning | what to do |
|---|---|---|
| `state initialisation failed: AGENT_NTFY_STORE='xx' is not a valid choice (only keychain / file / dpapi); leave it unset for the platform default: macOS keychain / Windows DPAPI / 0600 file elsewhere` | the environment names a store that does not exist | fix or unset `AGENT_NTFY_STORE` |
| `… the security command was not found (the keychain exists only on macOS); on other platforms set AGENT_NTFY_STORE=file (Linux) or dpapi (Windows)` | `keychain` store selected (or defaulted) on a machine without the macOS `security` command | set `AGENT_NTFY_STORE` as the line says |
| `… DPAPI is only available on Windows (this platform is xx); elsewhere use AGENT_NTFY_STORE=file or keychain` | `dpapi` store selected off Windows | same |
| `… decrypting topic pool file <home>/topics.dpapi failed (…): only the Windows user who encrypted it can decrypt it, and only on the same machine; after changing account / machine move it away and restart (a new pool is generated; the phone must re-subscribe)` | the pool file belongs to another Windows account or machine | the user moves the file away; the new pool needs `confirm-sub` again |
| `… topic pool file <home>/topics.json is not a JSON array of strings (…); if it was not edited by hand, move it away and restart (a new pool is generated; the phone must re-subscribe)` | the file store's pool file is damaged | same |
| `… reading topic pool file <path> failed: …` / `… writing topic pool file <path> failed: …` | permissions or disk problem on the pool file | the user checks the path; `add-slot` reports the write variant as rc 3 |
| `… reading keychain item 'AGENT_NTFY_TOPICS' failed (rc=N): …` / `… writing keychain item … failed` | the macOS keychain refused (locked, or access not granted to this Python) | the user unlocks / grants access in the keychain prompt on screen |
| `cannot listen for IPC: <path>: <error>` (`… Unix socket paths have a length limit (system-dependent); pick a shorter AGENT_NTFY_HOME` for the unix transport) | the endpoint could not be created | shorter `AGENT_NTFY_HOME`, or check what holds the path / port |
| `a daemon is already running (<path> accepts connections); not starting a second one` | something answers on the endpoint | `daemon --status`; with tcp, if `--status` says not running, an unrelated process holds the port: delete `daemon.port` and start again |
| `unauthenticated connection (token mismatch)` | a client presented a token that is not this daemon's (`daemon.port` out of date) | the client retries after `daemon --status`; see §4 |
