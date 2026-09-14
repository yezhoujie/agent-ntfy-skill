# agent-ntfy

English · [中文](README.zh-CN.md)

[![skills.sh](https://skills.sh/b/yezhoujie/agent-ntfy-skill)](https://skills.sh/yezhoujie/agent-ntfy-skill)

Let any AI coding CLI push the decisions it cannot make on its own to your phone through
[ntfy](https://ntfy.sh), and send your verdict — or any instruction — straight back into the agent's session.
It can also push one-way notifications to the same phone. No server, no fixed IP, no paid service, no
dependencies beyond Python 3.

This file is for the person installing it. The agent reads [SKILL.md](SKILL.md) and `references/`;
you never need to explain the tool to it.

## Contents

1. How it works
2. Requirements (2.1 platform support)
3. Install
4. Your two manual steps
5. First question, end to end
6. If nothing pops up on the phone (Android / MIUI checklist)
7. Security
8. Known limits (8.1 which agent CLIs work)
9. Environment variables
9.1 Remote mode and the per-project state file
10. Known behaviours
11. CLI reference
12. Versions and upgrading
13. Integration: keeping the skill in force for the whole session

## 1. How it works

```
agent ──ask (JSON on stdin)──▶ agent-ntfy ──local socket──▶ daemon ──HTTPS──▶ ntfy.sh ──▶ your phone
      ◀── reply on stdout ────            ◀────────────────        ◀── SSE ────         ◀── tap / type
                                                                      │
                                                          no question pending?
                                                                      ▼
                                                    injected into the agent's herdr pane
```

- The agent hands over a JSON with eight required fields; the CLI renders it into a fixed Markdown card with **one** button ("Accept recommended") and pushes it. The agent blocks until you tap or type; your reply is returned verbatim. With `notify` it can also push a one-way card (title + body, no button) and carry on.
- A resident **daemon** is the only ntfy subscriber. Anything you send while no question is pending is injected into the agent's session as an instruction prefixed with `[agent-ntfy remote] ` (this is the one part that needs herdr; §2 lists what works without it).
- Each **project** (the git toplevel of the directory the agent works in, otherwise that directory) leases one **slot** = one random ntfy topic out of a pool kept in the keychain, a DPAPI file or a 0600 file (§7). Replies are routed by topic. The lease also remembers the herdr pane from which the project last ran `ask`, `notify`, `slots`, `release` (no argument), `away on` or `away status`; that is where phone messages are injected.
- The channel carries text; it never interprets it, never answers for you, never dedupes.
- The local socket is a Unix socket on macOS / Linux and a loopback TCP port on Windows (§9, `AGENT_NTFY_IPC`).

## 2. Requirements

- Python 3.10 or newer — standard library only, nothing to `pip install`
- The [ntfy app](https://ntfy.sh) on your phone; no ntfy.sh account needed. Tested on Android (MIUI); ntfy also ships an iOS app, untested here
- Outbound HTTPS to `ntfy.sh` (or your own instance). The daemon does **not** read `http_proxy` / `https_proxy` or the system proxy; a TUN-style VPN that is transparent to processes is fine
- A POSIX shell for the examples in this file and in SKILL.md (`$(...)`, heredocs, `alias`). On Windows that means Git Bash or WSL; the daemon and the CLI themselves run natively (the interpreter is usually `python` there, not `python3`)
- [herdr](https://herdr.dev), recommended — see below

### 2.1 Platform support

| platform | status |
|---|---|
| macOS | The whole chain is tested on real machines: daemon, `ask` / `notify`, herdr injection, real cards on a real phone. Topic pool in the keychain |
| Linux | **Unit tests on CI only** (ubuntu-latest, Python 3.10 and 3.13). No end-to-end test in a real environment yet — pull requests welcome. Topic pool in a `0600` file; herdr has a Linux build |
| Windows 10 / 11 | **Unit tests on CI only** (windows-latest, Python 3.10 and 3.13, including a real DPAPI round trip, the loopback-TCP transport and a real detached daemon). No end-to-end test in a real environment yet — pull requests welcome. Runs natively; use Git Bash or WSL for the shell examples. Phone → agent injection through herdr is untested there: how herdr's pane shell splits the command line is not covered, and the CLI builds pane command lines with `subprocess.list2cmdline` (cmd.exe / MS C runtime quoting). Stopping the daemon or resubscribing while its reader thread is blocked costs an extra 0.2 s on Windows¹ |

¹ On Windows `shutdown()` does not wake a thread blocked in `recv()`, so the daemon waits 0.2 s for it before closing the socket for real.

### herdr: install it first if you can, and what works without it

[herdr](https://herdr.dev) is the terminal multiplexer this skill uses to deliver text *into* an agent's session (`brew install herdr` on macOS / Linux; see https://herdr.dev for other platforms). It is the only piece that is optional, and this is exactly what you keep and lose without it:

| works without herdr | needs herdr |
|---|---|
| The whole `ask` round trip: card on the phone → tap or type → reply on stdout → exit code; `notify` | Phone → agent messages when no question is waiting: an instruction you send on your own initiative, or a reply to a card that has already timed out or been cancelled |
| The daemon and every other subcommand: `confirm-sub`, `slots`, `release`, `add-slot`, `away` | `away on` starting the daemon in a pane and `confirm-sub` opening a pane for you (without herdr they fall back to `daemon --detach` and to asking you to run `confirm-sub` yourself) |
| Leases are per project either way; without herdr no pane is recorded on the lease, so there is nothing to inject into | |

Such a message is never dropped silently. The daemon answers on the phone with a receipt titled `[slotN] Message not delivered` whose body is `The lease on slot slotN has no target pane registered (the session that ran the command was not inside herdr). Run ask, notify, slots, release (no argument), away on or away status for this project from a herdr pane to register one, or release the slot.` followed by `What you just sent did not reach any agent.`, with `Release slot` / `Ignore` buttons.

Why herdr and nothing else: injecting means writing a line of text into the target agent's terminal (its PTY), and `herdr agent prompt` is the one generic way to do that for any agent CLI; this skill has no fallback mechanism.

## 3. Install

Into the current project (by default the skill lands in `./.agents/skills/agent-ntfy`, with a symlink from `./.claude/skills/agent-ntfy`; with a single non-universal agent selected via `-a <agent>` the CLI copies it into that agent's directory instead):

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy
```

For all projects at once, add `-g`: the files go to `~/.agents/skills/agent-ntfy` and `~/.claude/skills/agent-ntfy` becomes a symlink to them.

> **Warning about `-g`.** If `~/.claude/skills/agent-ntfy` already exists as a real directory (a copy you put there by hand), the `skills` CLI deletes it and replaces it with the symlink. Back it up first. (Read from the CLI's source; not something to try on a directory you care about.)

Any other way of putting `skills/agent-ntfy/` where your agent loads skills works just as well (`git clone` and copy the folder). To pin a version, install with a git ref: `npx skills add 'yezhoujie/agent-ntfy-skill#v0.1.0' --skill agent-ntfy` (§12).

The CLI is `scripts/agent_ntfy.py` inside that directory. Its own messages call it `agent-ntfy`; an alias
makes the commands below shorter:

```bash
alias agent-ntfy='python3 "<path to skills/agent-ntfy>/scripts/agent_ntfy.py"'
```

## 4. Your two manual steps

1. Install the skill (above). Once.
2. The first time a slot is used, subscribe its topic on your phone and tap the button on the test notification (§5, step 2). Once per slot.

Everything else is automatic: the topic pool is created on first use, the daemon can be started by the agent, leases are taken on demand.

## 5. First question, end to end

**Step 1 — start the daemon.** It must outlive the agent, so it runs on its own:

```bash
agent-ntfy daemon --detach        # any platform:  daemon: started in the background, pid 12345 (log ~/.agent-ntfy/daemon.log)
agent-ntfy daemon                 # inside herdr: run it in a spare pane instead, so it stays visible
agent-ntfy daemon --status        # daemon: pid 12345  subscription: connected  pending questions: 0  confirming: 0  slots: 5  transport: unix
```

Paths are shown with `~` here; the CLI prints them expanded. If `--detach` reports `daemon (pid 12345) not ready within 5 s, still starting; check later with agent-ntfy daemon --status, log …`, look at the log — one possible cause on macOS is a keychain dialog on screen on first run (the pool is read through the `security` command): answer it, then check `--status`. Want Chinese wording? Set `AGENT_NTFY_LANG=zh` in the shell you start it from: the daemon keeps the language of the environment that started it (when `away on` starts it for you, it passes the caller's language along). Never start the daemon as a background job of the agent's own shell: it would die with the agent. You do not have to do this step by hand: `away on` (§9.1) starts the daemon when none answers.

**Step 2 — confirm that your phone gets notifications for slot 1.** Run this in your own terminal (not through the agent: it prints the topic name, which is the password):

```
$ agent-ntfy confirm-sub slot1
topic for slot1: agent-ntfy-xxxxxxxxxxxxxxxxxxxx
subscribe URL: https://ntfy.sh/agent-ntfy-xxxxxxxxxxxxxxxxxxxx
Subscribe to the topic above in the ntfy app on your phone. Once subscribed, press Enter and I'll send a test notification with a button — when it pops up, tap the button and the check is done.
Press Enter once subscribed…
agent-ntfy: test notification sent — tap “Got it” in the phone's notification shade (within 600 s)…
✅ slot1 confirmed: the phone gets notifications; the agent can use it for questions from now on
```

Only the tap counts, and it must be the notification that popped up — tapping inside the app proves nothing about notifications (see §6). If nothing pops up within 10 minutes the command exits 2; fix the phone and run it again. When the agent itself runs `confirm-sub` inside herdr, it opens a new pane for you with exactly this dialogue and tells you which pane to look at; the topic name never enters the agent's output.

**Step 3 — ask yourself a question**, to see the round trip:

```bash
agent-ntfy ask <<'JSON'
{
  "title":       "Test: which dessert",
  "doing":       "Checking that agent-ntfy reaches this phone",
  "description": "This is the first question sent through agent-ntfy from this machine. Nothing depends on the answer.",
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

The phone shows the card: title `[<project dir>] Test: which dessert`, then bold section labels (`[Doing]`, `[Background]`, `[Blocker]`, `[Options]`, `[My recommendation]`, `[Your call]`), a rule, the closing hint and one button. The options are numbered `1\. Cake (recommended) → …` / `2\. Pie → …` in the Markdown source and separated by blank lines — an escaped period, which CommonMark renders as a plain `1.`, because the ntfy Android app turns a real ordered list into bullets and the numbers disappear (a client that does not render Markdown shows the backslash). Tap **Accept recommended** and the terminal prints `Cake`; type `pie, obviously` in the app's reply box instead and it prints `pie, obviously`. The card on the phone turns into `✅ Answered · …` with your reply on top and the question kept below it.

Leases belong to the project (§1). If you ran this test inside the directory your agent will work in, the agent simply reuses slot1 — nothing to do. If you ran it elsewhere, run `agent-ntfy release` there (no argument releases the slot leased by the current project); otherwise the agent would be handed slot2 — unconfirmed — and send you back to step 2 for it.

**Step 4 — hand it to the agent.** It reads SKILL.md on its own. When it hits a slot that is not confirmed yet, it exits with code 4 and asks you to run `confirm-sub slotN` (step 2) — inside herdr it opens that pane for you instead. That is the design, not a bug: the topic name must not pass through the agent's output.

**Notifications.** The agent can also send a one-way card that needs no answer:

```bash
agent-ntfy notify <<'JSON'
{"title": "Build finished", "body": "**Tests**: 483 passed.\n\nNothing to decide; just so you know.", "lang": "en"}
JSON
```

It prints `notification sent on slot1 (if the user replies, it arrives as an instruction)` and returns at once: no button, no waiting, exit codes 0 sent · 1 invalid input · 3 channel failure · 4 a human must act. It is allowed while a question is pending. The ntfy app has **one reply box per topic**, not per card: whatever you send while a question is pending is that question's reply; when nothing is pending it is injected into the agent's session (§2). Notifications count against the same ntfy.sh quota as questions (§8), so the agent is told not to chatter.

**Housekeeping.** Slots are leased until released (`agent-ntfy slots` to see who holds what — the project path, since when, and the pane — and `agent-ntfy release <slot>` to free one). Once all five are taken the agent asks you which to release or whether to `add-slot`; that is normal steady state.

## 6. If nothing pops up on the phone (Android / MIUI checklist)

The failure to know about: **the message reaches the server (HTTP 200), reaches the phone (it is listed
under the topic in the ntfy app), and still no notification appears — with no error anywhere.** From the
agent's side this looks exactly like "the human has not answered yet"; `ask` keeps waiting for a question
you do not know exists. The confirmation gate (§5 step 2) exists to catch this before the first real question.

Measured on a Xiaomi phone: with permissions misconfigured, priorities default / high / max all failed to
pop up; after fixing the settings all three worked. Raising the priority does not help; fix the settings.
Go through every line, they are independent:

- **Notification permission** for ntfy, and every notification *category* inside it enabled (MIUI keeps per-category switches)
- **Battery saver / background restrictions** for ntfy set to *no restrictions*
- **Autostart** allowed for ntfy
- **Lock-screen notifications** allowed for ntfy
- In the ntfy app, the topic is **not muted** and the app's own notification setting is on
- After changing anything, run `agent-ntfy confirm-sub slotN --again` and wait for the pop-up before tapping

Other Android ROMs have the same switches under different names; only MIUI has been tested.

## 7. Security

- **The topic name is the password.** Anyone who knows it can read every question, see every reply, and — with herdr — type instructions straight into your agent. There is no second lock by design (the channel does not filter content). Keep it off screenshots, out of chat, out of git.
- Topics are 20 random lowercase letters and digits after the prefix (about 2^103 possibilities). Where the pool is stored depends on the platform (`AGENT_NTFY_STORE`, §9): the **macOS keychain** (per-app authorisation); on **Windows** a DPAPI-encrypted file `~/.agent-ntfy/topics.dpapi` (decryptable only by the same Windows user on the same machine); elsewhere a plain `0600` file `~/.agent-ntfy/topics.json`. The last two can be read by any process running as your user — a wider boundary than the keychain; accept it or self-host ntfy. The lease file (`~/.agent-ntfy/leases.json`: slot numbers, holder ids, timestamps, pane ids), the per-project state file and the daemon log never contain topic names or message text.
- **Content travels in clear** through ntfy.sh. Questions describe your project; do not put secrets in them.
- To rotate all topics: on macOS delete the keychain item (account `agent-ntfy`, service `AGENT_NTFY_TOPICS`, e.g. `security delete-generic-password -a agent-ntfy -s AGENT_NTFY_TOPICS`); elsewhere delete `topics.json` / `topics.dpapi`. Then restart the daemon: a new pool is generated, old leases are discarded, every slot needs confirming again.
- **Do not create the keychain item by hand.** The program looks it up by account `agent-ntfy` *and* service `AGENT_NTFY_TOPICS`; an item with any other account name is invisible to it, so it would silently create a second pool while you believe yours is in use.

## 8. Known limits

- Linux and Windows have unit-test coverage only, no end-to-end run in a real environment (§2.1). Pull requests with a real-machine report are welcome.
- ntfy.sh's free tier allows about **250 messages per day per source IP**, shared by questions, notifications, "answered" updates, receipts and confirmations. Enough for normal use; if not, self-host ntfy and set `AGENT_NTFY_URL`.
- Messages are cached 12 hours on ntfy.sh; a phone offline longer than that misses them. The default `ask` timeout is 12 hours for the same reason.
- One button per card, 2–5 options, body ≤ 3584 bytes, title ≤ 960 bytes (`notify`: body ≤ 4096 bytes after rendering). Over-length input is rejected with the exact numbers, never truncated.
- The daemon ignores proxy environment variables (see §2).
- Phone → agent injection needs herdr (§2). It never checks whether the agent is busy (your CLI queues input); it does check that the pane still exists.

### 8.1 Which agent CLIs work

Two sides, two different answers.

- **Asking (agent → phone → agent)**: any agent CLI that can run a shell command, with Python 3.10 or newer on the machine. There is no list; the skill only needs stdin, stdout and an exit code.
- **Injection (phone → agent, needs herdr)**: whichever agent kinds herdr can host. On herdr 0.9.0 `herdr agent start --help` lists 23: pi, claude, codex, gemini, cursor, devin, agy, cline, omp, mastracode, opencode, copilot, kimi, kiro, droid, amp, grok, hermes, kilo, qodercli, qwen, maki, muse. The daemon reads the pane's agent kind and wakes the target accordingly: `claude` gets `herdr agent prompt` only (the text joins its current turn); `kimi` gets `prompt` followed by `ctrl+s` (without the key press it queues the text and does not read it while busy); every other kind gets `prompt` only, **untested**.

Tested on real sessions: **claude** (asking and injection, including replay after a network outage and injection after a daemon restart) and **kimi** (injection with wake-up; `ask` verified as far as leasing a slot). All other kinds are expected to work per herdr's documentation but have not been exercised.

## 9. Environment variables

| variable | default | effect |
|---|---|---|
| `AGENT_NTFY_HOME` | `~/.agent-ntfy` | state directory (dir 0700, files 0600 where the platform has such bits): `daemon.sock` or `daemon.port` (see `AGENT_NTFY_IPC`), `daemon.pid`, `daemon.log`, `leases.json`, and the topic pool file where one is used (`topics.json` / `topics.dpapi`, see `AGENT_NTFY_STORE`). With the Unix-socket transport keep the path short: the socket path has a system-dependent length limit; too deep and the daemon refuses to start with `cannot listen for IPC: … Unix socket paths have a length limit (system-dependent); pick a shorter AGENT_NTFY_HOME` |
| `AGENT_NTFY_LANG` | `en` | language of all fixed wording (card labels, button, receipts, CLI output, `--help`): `zh` or `en`. Any other value is an error, not a fallback. The agent can override it per question with the `lang` field. When the CLI itself opens a herdr pane (`away on` starting the daemon, `confirm-sub` opening the check for you), the command it types there carries `env AGENT_NTFY_LANG=<the caller's language>`, so the pane's own shell does not decide the wording; only a daemon or command you start by hand in a pane inherits that pane's environment |
| `AGENT_NTFY_TARGET` | `proj:<project root>` | the identity that leases a slot (this is what `slots` shows as the holder). Set it to share one slot across projects or to keep one apart; the same value always reuses the same slot |
| `AGENT_NTFY_URL` | `https://ntfy.sh` | another ntfy instance, e.g. self-hosted |
| `AGENT_NTFY_IPC` | `unix` on macOS / Linux, `tcp` on Windows | transport between the CLI and the daemon. `unix`: a Unix socket `daemon.sock`, protected by file permissions. `tcp`: a loopback TCP port; `daemon.port` holds two lines — the port and a random token — and every request's first line carries the token (any local process could otherwise connect). A leftover endpoint file is treated as stale unless a connection to it succeeds ("port accepts a connection ⇒ an instance is already running"); if an unrelated process happens to hold that port, delete `daemon.port` and start the daemon again. `unix` is rejected on Windows; any other value is an error |
| `AGENT_NTFY_STORE` | `keychain` on macOS, `dpapi` on Windows, `file` elsewhere | where the topic pool lives: `keychain` (macOS `security` command; on other platforms the error says `the security command was not found (the keychain exists only on macOS); on other platforms set AGENT_NTFY_STORE=file (Linux) or dpapi (Windows)`), `file` (`topics.json`, mode 0600), `dpapi` (`topics.dpapi`, Windows only — elsewhere `DPAPI is only available on Windows`). A pool file that cannot be decrypted (other user / other machine) or is not a JSON array of strings is reported with the file path; move the file away and restart to get a fresh pool (the phone must re-subscribe). See §7 for what each choice protects against |
| `AGENT_NTFY_KEYCHAIN` | `AGENT_NTFY_TOPICS` | keychain service name of the topic pool (macOS, `keychain` store only) |
| `AGENT_NTFY_TOPIC_PREFIX` | `agent-ntfy` | prefix of newly generated topic names (`<prefix>-<20 random chars>`); letters, digits, `-`, `_`, at most 40 |
| `HERDR_ENV`, `HERDR_PANE_ID` | set by herdr | detected, never set by you: inside herdr, `ask`, `notify`, `slots`, `release` (no argument), `away on` and `away status` record the current pane on the project's lease, and phone messages are injected there (`confirm-sub`, `release <slot>`, `add-slot`, `daemon`, `away off` do not touch it) |

`--home <dir>` on the command line (before the subcommand) overrides `AGENT_NTFY_HOME`.

### 9.1 Remote mode and the per-project state file

The skill does not decide *when* the agent should ask on the phone; that is your policy (a rule in
your agent's configuration). What the skill gives that policy is a switch and a place to read it:

```
agent-ntfy away on        # you are leaving: decisions should go to the phone
agent-ntfy away off       # you are back
agent-ntfy away status    # in words; add --json for the raw file
```

`away on` is a one-stop command. It starts the daemon if none answers (inside herdr in a new pane, otherwise with `--detach`), makes sure the project has a confirmed slot or that a confirmed idle slot is available (if not, inside herdr it opens a confirmation pane and tells the agent which pane you should look at; outside herdr it exits 4 and names the `confirm-sub` command to run), and only then creates `<project root>/.agent-ntfy/` (project root = the git toplevel, else the current directory) with a self-ignoring `.gitignore` and a `state.json`:

```json
{"away": true, "slot": "slot2", "confirmed": true, "target": "proj:/path/to/project", "updated": "2026-09-13T21:04:11+08:00"}
```

`slot` / `confirmed` / `target` are kept current by `ask`, `notify`, `confirm-sub`, `release` and `away` — but only in
projects where the directory already exists, so nothing is written into projects that never enabled
remote mode. `away status` asks the daemon for its leases and corrects the file when the two disagree (it then
prints `state file corrected from the daemon's leases`); with no daemon it prints the file and says so. The topic name is never stored there. While
`away` is `true`, `ask` and `notify` only use confirmed slots — nobody is at the keyboard to confirm a new one.

Run `away status --json` (the form meant for the agent) from the agent's herdr pane or a process started
from it: like `ask` / `notify` / `slots` it records the current pane on the lease, and run from elsewhere it would point
phone messages at the wrong pane. A typical rule reads: *if `.agent-ntfy/state.json` says `away: true`, use
`agent-ntfy ask` for anything that needs my decision; run it in the background (a foreground tool call is
killed after minutes and the card is cancelled); `release` when done.*

## 10. Known behaviours

Observed on a real phone; none is a bug.

- **Daemon restarted, old receipt on the phone.** Tapping a button on a receipt sent by the previous daemon process still performs the action, but you get a new short message saying so; the old card is not updated in place. Delete it by hand.
- **Network outage.** Shorter than about 90 s the daemon does not even notice (the connection resumes). Longer, it reconnects with backoff, and messages you sent meanwhile are replayed once, not duplicated. Once it has been down 60 s or failed to reconnect 3 times, a waiting `ask` prints an `agent-ntfy: note: …` line and keeps waiting.
- **Replies starting with `-`** (`-v`, `--help`, `- item`) are injected as-is; nothing is parsed as an option.
- **Stopping the daemon**: do not send from the phone while it shuts down. Messages consumed in that window get a best-effort receipt (`The daemon is stopping; your message was not delivered. Please resend later.`); if even that fails they are gone (the daemon does not replay history on start).
- **Cold start does not replay.** Messages sent while no daemon was running are not delivered later; the phone keeps them, the agent never sees them.
- **A claude target started in a directory it does not trust yet** sits at the trust dialog; injected text waits there until someone answers the dialog.
- **Two taps** on the same card, or tap-then-type, produce two messages. The first closes the question; the second is injected as an instruction. Agents are told to take the last one.
- **Very short `--timeout`.** With a timeout under roughly 90 s the card on the phone may never flip to `⌛ Timed out · …` (observed: a 5 s timeout did not flip, 90 s did; the cause has not been established). The exit code and everything after it are unaffected; the default timeout is 12 hours anyway.
- **Ordered lists become bullets on Android.** The ntfy Android app renders Markdown `1.` lists as bullet points, which is why the card writes option numbers as `1\.` (§5). The same applies to any ordered list in a `notify` body.

## 11. CLI reference

Output of `AGENT_NTFY_LANG=en python3 scripts/agent_ntfy.py --help` and `<subcommand> --help` (`slots` and `add-slot` take no options), with the home directory shown as `~`:

```
usage: agent-ntfy [-h] [--home HOME]
                  {ask,notify,daemon,slots,release,confirm-sub,add-slot,away} ...

Push decisions that need a human to your phone via ntfy.sh, and bring the
verdict back

positional arguments:
  {ask,notify,daemon,slots,release,confirm-sub,add-slot,away}
    ask                 block and ask: reads the question JSON from stdin
    notify              one-way notification (no buttons, no waiting); reads
                        {"title", "body"} JSON from stdin
    daemon              the resident subscriber process
    slots               show the slot pool and leases
    release             release a lease
    confirm-sub         reachability check: verify the phone gets
                        notifications for this slot (shows the topic name when
                        run in a terminal; when an agent runs it inside herdr
                        it opens a pane for the user)
    add-slot            add a slot
    away                remote-mode switch: on is one-stop (starts the daemon,
                        makes sure a usable slot exists, then writes .agent-
                        ntfy/state.json at the project root for the agent to
                        read; no topic name in it)

options:
  -h, --help            show this help message and exit
  --home HOME           state directory (default ~/.agent-ntfy)

usage: agent-ntfy ask [-h] [--timeout TIMEOUT]

options:
  -h, --help         show this help message and exit
  --timeout TIMEOUT  seconds to wait for a reply (default 12 hours)

usage: agent-ntfy notify [-h]

options:
  -h, --help  show this help message and exit

usage: agent-ntfy daemon [-h] [--detach | --status | --stop]

options:
  -h, --help  show this help message and exit
  --detach    detach from the session and run in the background
  --status    show daemon status
  --stop      stop the daemon

usage: agent-ntfy release [-h] [slot]

positional arguments:
  slot        slot to release; omit to release the one leased by the current
              target

options:
  -h, --help  show this help message and exit

usage: agent-ntfy confirm-sub [-h] [--again] [--subscribed] [--show-topic]
                              [--timeout TIMEOUT]
                              slot

positional arguments:
  slot               slot to confirm

options:
  -h, --help         show this help message and exit
  --again            re-confirm an already confirmed slot (after changing
                     phones)
  --subscribed       user already subscribed: skip showing the topic and send
                     the test notification right away (works outside a
                     terminal)
  --show-topic       only print the topic name and exit, send nothing (it will
                     land in the caller's output)
  --timeout TIMEOUT  seconds to wait for the button tap (default 600)

usage: agent-ntfy away [-h] [--json] {on,off,status}

positional arguments:
  {on,off,status}  on / off / status

options:
  -h, --help       show this help message and exit
  --json           with status: print state.json verbatim (for the agent)
```

Exit codes of `ask`: 0 reply on stdout · 1 invalid input, nothing sent · 2 timeout · 3 channel failure (daemon not running, connection lost, publish failed; stderr says whether the message went out) · 4 a human must act (unconfirmed slot, all slots leased, target already waiting) · 130 Ctrl-C. `notify`: 0 sent · 1 invalid input · 3 channel failure · 4 a human must act (no 2: it does not wait). `confirm-sub`: 0 confirmed (or the check was started in a herdr pane) · 1 unknown slot · 2 no Enter or no tap within the timeout · 3 channel failure · 4 not a terminal and no herdr pane possible (and no `--subscribed`), stdin ended before Enter, or slot busy · 130 Ctrl-C. The stderr text for each case is in [references/failures.md](references/failures.md).

Set `AGENT_NTFY_LANG=zh` to get the same help and messages in Chinese. The daemon log is written in Chinese regardless.

## 12. Versions and upgrading

Versions are git tags `vX.Y.Z`; what changed is in [CHANGELOG.md](../../CHANGELOG.md). The `skills` CLI and skills.sh do not read a version number — an install is a snapshot of the repository content, and `npx skills update` refreshes it (`-g` for global installs, `-p` for the current project). To stay on a release, install with the tag as git ref; per the `skills` CLI documentation `update` then stays on that ref:

```bash
npx skills add 'yezhoujie/agent-ntfy-skill#v0.1.0' --skill agent-ntfy
```

**Upgrading a machine that already runs a daemon** — do the steps in this order:

1. Stop the running daemon **with the CLI you have now**: `agent-ntfy daemon --stop`. If you already replaced the files, send it `kill -TERM <pid>` instead (the pid is in `~/.agent-ntfy/daemon.pid`). Reason: since 0.1.0 `--stop` asks the daemon over its socket; a daemon from an earlier version does not know that command, so the new CLI reports `did not acknowledge the stop` and exits 1.
2. Update the files: `npx skills update` (or run the install command again, or copy the directory).
3. Start the new daemon: `agent-ntfy daemon --detach`, then `agent-ntfy daemon --status` should show `transport: unix` (or `tcp` on Windows). A restart is required in any case: an earlier daemon ignores the fields the new CLI sends.
4. Run `agent-ntfy slots`. Leases taken before 0.1.0 show a pane id such as `wG:p1` as holder instead of `proj:<path>`; free them with `agent-ntfy release <slot>` (`release` without argument only finds the current project's lease).
5. In the herdr pane your agent works in, run `agent-ntfy slots` (or `ask` / `notify` / `away status`) so the project's lease records that pane; phone messages are injected there.

Nothing else migrates: the state directory layout and `state.json` are unchanged, and the defaults (`AGENT_NTFY_IPC`, `AGENT_NTFY_STORE`) reproduce the previous behaviour on macOS.

## 13. Integration: keeping the skill in force for the whole session

The skill only provides the calls — `ask`, `notify`, `away` — and deliberately never decides *when* to use
them (SKILL.md, "When to use"). Left alone, an agent uses agent-ntfy only when it happens to remember the
skill exists, which is not what you want while you are away. The trigger policy belongs in the agent's
**standing instructions** — the file it loads in every session — and it has to cover four moments:

1. **Session start / context reset**: read `<project root>/.agent-ntfy/state.json` (`away status --json`);
   `away: true` means the human is away and every decision goes to the phone from now on.
2. **The human leaves** ("I'm leaving, send it to my phone"): run `away on` while they are still at the
   keyboard and relay its output — the two taps on the phone (subscribe, press the button) cannot be done
   for them.
3. **While away**: every question, confirmation or authorization becomes an `ask` (run in the background,
   one at a time, act on the exit code); phone messages arrive with the `[agent-ntfy remote] ` prefix;
   `notify` is reserved for answering a question asked from the phone and for major events that need no
   decision — task finished, an error, the task cannot continue — never for progress chatter (quota, §8).
4. **The human is back**: `release`, then `away off`; the daemon keeps running.

A ready-made rule that does exactly this ships with the skill: [`examples/remote-mode-rule.md`](examples/remote-mode-rule.md)
(English) and [`examples/remote-mode-rule.zh-CN.md`](examples/remote-mode-rule.zh-CN.md) (Chinese). It also covers
teams of agent sessions (only the session that talks to the human holds remote mode). For Claude Code, rules in
`~/.claude/rules/` are injected into every session:

```bash
cp ~/.claude/skills/agent-ntfy/examples/remote-mode-rule.md ~/.claude/rules/agent-ntfy-remote-mode.md
```

For other agents, put it wherever that agent loads its standing instructions. Adjust the `<skill dir>`
path at the top and the trigger phrases ("I'm leaving", "I'm back") to your own habits; the rest is
product behaviour and should stay as written.
