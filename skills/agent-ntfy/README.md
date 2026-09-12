# agent-ntfy

English · [中文](README.zh-CN.md)

Let any AI coding CLI push the decisions it cannot make on its own to your phone through
[ntfy](https://ntfy.sh), and send your verdict — or any instruction — straight back into the agent's session.
No server, no fixed IP, no paid service, no dependencies beyond Python 3.

This file is for the person installing it. The agent reads [SKILL.md](SKILL.md) and `references/`;
you never need to explain the tool to it.

## Contents

1. How it works
2. Requirements
3. Install
4. Your two manual steps
5. First question, end to end
6. If nothing pops up on the phone (Android / MIUI checklist)
7. Security
8. Known limits (8.1 which agent CLIs work)
9. Environment variables
10. Known behaviours
11. CLI reference

## 1. How it works

```
agent ──ask (JSON on stdin)──▶ agent-ntfy ──unix socket──▶ daemon ──HTTPS──▶ ntfy.sh ──▶ your phone
      ◀── reply on stdout ────            ◀───────────────        ◀── SSE ────         ◀── tap / type
                                                                    │
                                                        no question pending?
                                                                    ▼
                                                  injected into the agent's herdr pane
```

- The agent hands over a JSON with eight required fields; the CLI renders it into a fixed card with **one** button ("Accept recommended") and pushes it. The agent blocks until you tap or type; your reply is returned verbatim.
- A resident **daemon** is the only ntfy subscriber. Anything you send while no question is pending is injected into the agent's session as a plain instruction (this is the one part that needs herdr; §2 lists what works without it).
- Each agent (a herdr pane, or a configured target id) leases one **slot** = one random ntfy topic out of a pool kept in the macOS keychain. Replies are routed by topic.
- The channel carries text; it never interprets it, never answers for you, never dedupes.

## 2. Requirements

- macOS (the topic pool lives in the keychain; there is no Linux backend yet)
- Python 3.10 or newer — standard library only, nothing to `pip install`
- The [ntfy app](https://ntfy.sh) on your phone; no ntfy.sh account needed. Tested on Android (MIUI); ntfy also ships an iOS app, untested here
- Outbound HTTPS to `ntfy.sh` (or your own instance). The daemon does **not** read `http_proxy` / `https_proxy` or the system proxy; a TUN-style VPN that is transparent to processes is fine
- [herdr](https://herdr.dev), recommended — see below

### herdr: install it first if you can, and what works without it

[herdr](https://herdr.dev) is the terminal multiplexer this skill uses to deliver text *into* an agent's session (`brew install herdr`; docs at https://herdr.dev). It is the only piece that is optional, and this is exactly what you keep and lose without it:

| works without herdr | needs herdr |
|---|---|
| The whole `ask` round trip: card on the phone → tap or type → reply on stdout → exit code | Phone → agent messages when no question is waiting: an instruction you send on your own initiative, or a reply to a card that has already timed out or been cancelled |
| The daemon and every other subcommand: `confirm-sub`, `slots`, `release`, `add-slot` | |
| The asking agent is identified by `AGENT_NTFY_TARGET` (unset: hostname + session id) instead of a herdr pane id, and the `[tag]` in card titles is the slot name rather than the pane id | |

Such a message is never dropped silently. The daemon answers on the phone with a receipt titled `[slotN] Message not delivered` whose body is `This slot's target <target> is not inside herdr (<why>); nothing to inject into.` followed by `What you just sent did not reach any agent.`, with `Release slot` / `Ignore` buttons.

Why herdr and nothing else: injecting means writing a line of text into the target agent's terminal (its PTY), and `herdr agent prompt` is the one generic way to do that for any agent CLI; this skill has no fallback mechanism.

## 3. Install

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy -g
```

This copies `skills/agent-ntfy/` into your agent's global skills directory. Any other way of putting that
directory where your agent loads skills works just as well (`git clone` and copy the folder).

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
agent-ntfy daemon --detach        # anywhere:  daemon: started in the background, pid 12345 (log ~/.agent-ntfy/daemon.log)
agent-ntfy daemon                 # inside herdr: run it in a spare pane instead, so it stays visible
agent-ntfy daemon --status        # daemon: pid 12345  subscription: connected  pending questions: 0  confirming: 0  slots: 5
```

Paths are shown with `~` here; the CLI prints them expanded. If `--detach` reports `not ready within 5 s, still starting (keychain prompt?)`, look at the screen for a keychain dialog (the pool is read through the `security` command), answer it, then check `--status`. Want Chinese wording? Set `AGENT_NTFY_LANG=zh` in your shell before step 1: the daemon keeps the language of the shell that started it. Never start the daemon as a background job of the agent's own shell: it would die with the agent.

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

Only the tap counts, and it must be the notification that popped up — tapping inside the app proves nothing about notifications (see §6). If nothing pops up within 10 minutes the command exits 2; fix the phone and run it again.

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

The phone shows the card. Tap **Accept recommended** and the terminal prints `Cake`; type `pie, obviously` in the app's reply box instead and it prints `pie, obviously`. The card on the phone turns into `✅ Answered · …` with your reply on top and the question kept below it.

Then run `agent-ntfy release` (no argument releases the slot leased by this shell). The test leased slot1 to your terminal session; the agent is a different identity and would otherwise be handed slot2 — unconfirmed — and send you back to step 2 for it.

**Step 4 — hand it to the agent.** It reads SKILL.md on its own. When it hits a slot that is not confirmed yet, it exits with code 4 and asks you to run `confirm-sub slotN` (step 2) — that is the design, not a bug: the topic name must not pass through the agent's output.

**Housekeeping.** Slots are leased until released (`agent-ntfy slots` to see who holds what, `agent-ntfy release <slot>` to free one). Once all five are taken the agent asks you which to release or whether to `add-slot`; that is normal steady state.

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
- Topics are 20 random lowercase letters and digits after the prefix (about 2^103 possibilities), stored only in the macOS keychain. The lease file (`~/.agent-ntfy/leases.json`) and the daemon log contain slot numbers only, never topic names, never message text.
- **Content travels in clear** through ntfy.sh. Questions describe your project; do not put secrets in them.
- To rotate all topics, delete the keychain item (account `agent-ntfy`, service `AGENT_NTFY_TOPICS`, e.g. `security delete-generic-password -a agent-ntfy -s AGENT_NTFY_TOPICS`) and restart the daemon: a new pool is generated, old leases are discarded, every slot needs confirming again.
- **Do not create the keychain item by hand.** The program looks it up by account `agent-ntfy` *and* service `AGENT_NTFY_TOPICS`; an item with any other account name is invisible to it, so it would silently create a second pool while you believe yours is in use.

## 8. Known limits

- macOS only (keychain). The storage layer is one class; a Linux backend is possible but not written.
- ntfy.sh's free tier allows about **250 messages per day per source IP**, shared by questions, "answered" updates, receipts and confirmations. Enough for normal use; if not, self-host ntfy and set `AGENT_NTFY_URL`.
- Messages are cached 12 hours on ntfy.sh; a phone offline longer than that misses them. The default `ask` timeout is 12 hours for the same reason.
- One button per card, 2–5 options, body ≤ 3584 bytes, title ≤ 960 bytes. Over-length input is rejected with the exact numbers, never truncated.
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
| `AGENT_NTFY_HOME` | `~/.agent-ntfy` | state directory: `daemon.sock`, `daemon.pid`, `daemon.log`, `leases.json` (dir 0700, files 0600). Keep the path short — Unix socket paths are limited to 104 bytes on macOS; too deep and the daemon refuses to start with `can't bind the socket … pick a shorter AGENT_NTFY_HOME` |
| `AGENT_NTFY_LANG` | `en` | language of all fixed wording (card labels, button, receipts, CLI output, `--help`): `zh` or `en`. Any other value is an error, not a fallback. The agent can override it per question with the `lang` field |
| `AGENT_NTFY_TARGET` | `host:<hostname>\|sid:<session id>` | outside herdr: a stable identity for the asking agent (this is what `slots` shows as the lease holder); the same value reuses the same slot. Inside herdr the pane id is used and this is ignored |
| `AGENT_NTFY_URL` | `https://ntfy.sh` | another ntfy instance, e.g. self-hosted |
| `AGENT_NTFY_KEYCHAIN` | `AGENT_NTFY_TOPICS` | keychain service name of the topic pool |
| `AGENT_NTFY_TOPIC_PREFIX` | `agent-ntfy` | prefix of newly generated topic names (`<prefix>-<20 random chars>`); letters, digits, `-`, `_`, at most 40 |
| `HERDR_ENV`, `HERDR_PANE_ID` | set by herdr | detected, never set by you: inside herdr the pane id is the lease holder and the `[tag]` in card titles |

`--home <dir>` on the command line (before the subcommand) overrides `AGENT_NTFY_HOME`.

## 10. Known behaviours

Observed on a real phone; none is a bug.

- **Daemon restarted, old receipt on the phone.** Tapping a button on a receipt sent by the previous daemon process still performs the action, but you get a new short message saying so; the old card is not updated in place. Delete it by hand.
- **Network outage.** Shorter than about 90 s the daemon does not even notice (the connection resumes). Longer, it reconnects with backoff, and messages you sent meanwhile are replayed once, not duplicated. Once it has been down 60 s or failed to reconnect 3 times, a waiting `ask` prints an `agent-ntfy: note: …` line and keeps waiting.
- **Replies starting with `-`** (`-v`, `--help`, `- item`) are injected as-is; nothing is parsed as an option.
- **Stopping the daemon**: do not send from the phone while it shuts down. Messages consumed in that window get a best-effort receipt (`The daemon is stopping; your message was not delivered. Please resend later.`); if even that fails they are gone (the daemon does not replay history on start).
- **Cold start does not replay.** Messages sent while no daemon was running are not delivered later; the phone keeps them, the agent never sees them.
- **A claude target started in a directory it does not trust yet** sits at the trust dialog; injected text waits there until someone answers the dialog.
- **Two taps** on the same card, or tap-then-type, produce two messages. The first closes the question; the second is injected as an instruction. Agents are told to take the last one.

## 11. CLI reference

```
usage: agent-ntfy [-h] [--home HOME]
                  {ask,daemon,slots,release,confirm-sub,add-slot} ...

Push decisions that need a human to your phone via ntfy.sh, and bring the
verdict back

positional arguments:
  {ask,daemon,slots,release,confirm-sub,add-slot}
    ask                 block and ask: reads the question JSON from stdin
    daemon              the resident subscriber process
    slots               show the slot pool and leases
    release             release a lease
    confirm-sub         reachability check: verify the phone gets
                        notifications for this slot (run it in a terminal by
                        default; it shows the topic name)
    add-slot            add a slot

options:
  -h, --help            show this help message and exit
  --home HOME           state directory (default ~/.agent-ntfy)
```

Subcommand help, condensed (each `<subcommand> --help` prints the full argparse layout):

```
usage: agent-ntfy ask [-h] [--timeout TIMEOUT]
  --timeout TIMEOUT  seconds to wait for a reply (default 12 hours)

usage: agent-ntfy daemon [-h] [--detach | --status | --stop]
  --detach    detach from the session and run in the background
  --status    show daemon status
  --stop      stop the daemon

usage: agent-ntfy confirm-sub [-h] [--again] [--subscribed] [--show-topic] [--timeout TIMEOUT] slot
  --again            re-confirm an already confirmed slot (after changing phones)
  --subscribed       user already subscribed: skip showing the topic and send the test notification right away (works outside a terminal)
  --show-topic       only print the topic name and exit, send nothing (it will land in the caller's output)
  --timeout TIMEOUT  seconds to wait for the button tap (default 600)

usage: agent-ntfy release [-h] [slot]     slot to release; omit to release the one leased by the current target
```

Exit codes of `ask`: 0 reply on stdout · 1 invalid input, nothing sent · 2 timeout · 3 channel failure (daemon not running, connection lost, publish failed; stderr says whether the message went out) · 4 a human must act (unconfirmed slot, all slots leased, target already waiting) · 130 Ctrl-C. `confirm-sub`: 0 confirmed · 1 unknown slot · 2 no tap in time · 3 channel failure · 4 not a terminal (and no `--subscribed`) or slot busy. The stderr text for each case is in [references/failures.md](references/failures.md).

Set `AGENT_NTFY_LANG=zh` to get the same help and messages in Chinese. The daemon log is written in Chinese regardless.
