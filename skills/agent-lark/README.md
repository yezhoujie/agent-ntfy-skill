# agent-lark

English · [中文](README.zh-CN.md)

This file is for the person installing it. The agent reads [SKILL.md](SKILL.md) and `references/`; you never need to explain the tool to it. It is the Feishu sibling of `agent-ntfy` in the same repository; how the two compare is in the [repository README](../../README.md).

## Contents

1. What it is
2. Requirements
3. Install
4. Setup, once per machine (4.1 by hand · 4.2 through your agent · 4.3 scopes · 4.4 credentials)
5. Using it: what you say, what the agent does
6. Day-to-day (6.1 things you ask the agent for · 6.2 the few commands you run yourself)
7. When something goes wrong
8. Security and limits
9. Upgrading
10. Make it stick: the rule for your agent

## 1. What it is

Your AI coding agent hits a decision it cannot make alone while you are away from the keyboard. Instead of waiting, it pushes the question to your phone as a Feishu card: one button per option, its own recommendation, and a hint. You tap — or type a sentence in the group — and the answer lands back in the very agent session that asked, with all its context. Messages, photos and voice notes you send on your own reach that session too. No server, no public URL: a small daemon on your machine dials out to Feishu; the Feishu app is created by scanning a QR code with your phone, no workspace-admin approval needed.

```
agent ──question──▶ daemon ──▶ Feishu ──▶ your phone: tap / tick / type ──▶ back to the agent, verbatim
```

One project (the directory the agent works in) gets one Feishu group; whichever group you speak in is the project you are speaking to.

## 2. Requirements

- **Platform**: macOS is tested end to end on real machines; Linux and Windows have unit tests on CI only (Windows runs natively, use Git Bash or WSL for the shell examples) — pull requests with a real-machine report are welcome.
- Node.js 22 or newer. The CLI is one self-contained file (`dist/cli.mjs`): nothing to `npm install`, nothing to build.
- A Feishu / Lark account — a personal one is enough — and outbound access to Feishu from the machine the daemon runs on.
- **[herdr](https://herdr.dev) is optional** (`brew install herdr` on macOS / Linux). What depends on it, in three lines:
  - The whole question round trip (card → tap or type → answer back to the agent), notifications and files work **without** herdr.
  - Messages you send on your own initiative — an instruction, a photo, a voice note, a reply to an old card — are typed into the agent's terminal by herdr; without it they cannot be delivered, and you get a receipt card in the group saying so.
  - The 🔔 *waiting for you* card (pushed when the agent is stuck on a prompt only you can answer) also needs herdr; so does the pane the agent opens for you in §4.2.

## 3. Install

Into the current project (the skill lands in `./.agents/skills/agent-lark`, with a symlink from `./.claude/skills/agent-lark`; with a single non-universal agent selected via `-a <agent>` the CLI copies it into that agent's directory instead):

```bash
npx skills add yezhoujie/agent-remote-communication-skills --skill agent-lark
```

For all projects at once, add `-g`: the files go to `~/.agents/skills/agent-lark` and `~/.claude/skills/agent-lark` becomes a symlink to them. **Warning about `-g`**: if `~/.claude/skills/agent-lark` already exists as a real directory, the `skills` CLI deletes it and replaces it with the symlink — back it up first. Any other way of putting `skills/agent-lark/` where your agent loads skills works too (`git clone` and copy the folder); to pin a version, install with a git ref (§9).

The CLI is `dist/cli.mjs` inside that directory; it calls itself `agent-lark`. You will type it rarely (§6.2), but an alias helps: `alias agent-lark='node "<path to skills/agent-lark>/dist/cli.mjs"'` (the file is executable, so a symlink on your `PATH` works as well).

## 4. Setup, once per machine

Once per machine the Feishu app is created (or an app you already have is reused) and its credentials are stored. Nothing else is ever set up by hand: groups, the daemon and the switch are the agent's job (§5). Two ways lead here.

### 4.1 By hand

```bash
agent-lark setup
```

Every line is printed in Chinese and English side by side (the English half is shown here). On a terminal it opens with a menu:

```
How do you want to connect to Feishu?
  1) Create a new app by QR code (scan it with Feishu)
  2) Reuse an app you already have (enter its App ID and App Secret)
Choose [1/2]:
```

**1 — new app by QR code.** The code is drawn in the terminal and the same link is printed as a line of text right under it, in case the drawing renders badly. Scan it with Feishu on your phone; the confirmation page lists the permissions being requested (§4.3); approve, and the app exists. The code is valid for a few minutes (the expiry time is printed); if it expires, run `setup` again. On success:

```
✅ App linked; credentials saved to macOS Keychain (service: agent-lark) (the secret never appears in any output).
Next: back in your agent session, say "turn remote mode on" or type /agent-lark on — the agent starts the daemon and binds the group from its own pane; nothing to run by hand.
```

**2 — reuse an app you already have** (`agent-lark setup --reuse` goes here directly). It asks for the App ID (`cli_…`, from Developer console → Credentials & Basic Info) and the App Secret — typed blind, never echoed, never shown afterwards, not even inside an error message — checks the pair against Feishu once, stores it, and prints the scopes, the event and the callback you must enable **by hand** for such an app (§4.3), then the same `Next:` line. A pair Feishu rejects is asked again; three refusals stop with nothing stored.

**Whichever way:** credentials go to the OS keychain where one is reachable, else a `0600` file (§4.4). Run `setup` again later and it only says `Credentials already exist (from …)`. `setup --update` rescans the QR code to re-authorize the same app (that is how a missing scope is added after a QR-code setup); `setup --reset` forgets the stored credentials first, so `agent-lark setup --reset --reuse` (or `--reset` alone, for the QR code) switches to another app.

### 4.2 Through your agent

Type `/agent-lark setup` to your agent (or just say "set up agent-lark" / "turn remote mode on" — with no credentials yet it goes through setup first). It asks you which way, and never picks for you:

- **New app by QR code**: the agent runs the setup and gives you the link (or a QR image); you scan it with Feishu and it reports the outcome.
- **Reuse an app you already have**: the App ID and, above all, the App Secret must not pass through the agent. Inside [herdr](https://herdr.dev) the agent opens a new terminal pane right below its own and the focus moves there: type the App ID and the Secret in that pane; when you are done the agent picks up the result by itself. Outside herdr the agent gives you one command to run **in a terminal window of your own** (Terminal, iTerm, …) — not inside the agent session; run it, then tell the agent you are done.

Either way, setup only stores credentials and the agent reports the outcome; turning remote mode on is the next thing you say (§5). How the hand-off works underneath is in [SKILL.md](SKILL.md), "Invoked with an argument".

### 4.3 Scopes

A QR-code setup asks for these on the confirmation page; an app you reuse must have the same ones enabled in the developer console (app → Permissions & Scopes), plus the event `im.message.receive_v1` and the card callback `card.action.trigger`, both delivered over Feishu's *long connection* — then publish a version, or nothing takes effect:

```
im:message   im:message:send_as_bot   im:message.group_msg   im:chat   im:resource   im:message.urgent   speech_to_text:speech
```

`im:message.urgent` is for the urgent flag; `speech_to_text:speech` is for voice notes (paid tenants only, §7). Missing one after a QR-code setup? `agent-lark setup --update` adds it.

### 4.4 Credentials

Three places, highest first: `AGENT_LARK_APP_ID` / `AGENT_LARK_APP_SECRET` in the environment (a runtime override), the OS keychain (what `setup` writes; macOS `security`, Linux `secret-tool`, a DPAPI-encrypted file on Windows), or `~/.config/agent-lark/credentials.json` with mode `0600`. Nothing else is read — no env file. `agent-lark status` shows which one is in use **and never prints a value**. Details and every variable: [references/daemon.md](references/daemon.md) §7.

## 5. Using it: what you say, what the agent does

**Turning it on.** Say "turn remote mode on" / "I'm leaving, send it to my phone", or type `/agent-lark on`. The agent runs `away on --name "<task>"` from its own terminal pane (that pane is where your phone messages will be typed in): the daemon starts if it is not running, a Feishu group named `<task> [<project dir>]` is created with you in it, and the switch is on. **If the project had a group before** (an earlier task ended, or the local records were lost), the agent does not create a second one: it lists the old groups and **asks you** which one to take back — renamed to the new task — or whether to create a new one. Outside herdr it also tells you that messages from the phone will not be typed into its session.

**While you are away — what the phone shows.**

- **A question** is a blue card titled `🤔 [<project dir>] <title>`: what the agent is doing, the background, what is blocked, the numbered options with the recommended one marked, its reasoning, and one button per option. Tap a button, or **just type in the group** — while a question is pending, the first message you send there *is* the answer, whatever it says. The card turns green (`✅ … · Answered`) with your reply on top. Nobody answers ⇒ grey after the timeout (`⌛ … · Timed out`, 12 hours by default) or when the agent gave up (`⚠️ … · Cancelled`). Three variations: a form with checkboxes and a **Submit** button when several answers may apply; a red button behind a confirm dialog for an irreversible option (the agent may never recommend such an option); a red header plus Feishu's in-app *urgent* ping when the agent flags a question urgent.
- **A notification** is a light-blue `📣` card with no button and no state: it never changes colour; replying to it is an instruction like any other message. The agent may also drop an image or a file into the group.
- **Anything you send yourself** — while no question is pending — is typed into the agent's session as an instruction, prefixed `[agent-lark remote] `; once it has landed your message gets a `Get` reaction. Photos and files are saved on the machine and the agent is given their paths. A voice note is transcribed only on a paid Feishu tenant (§7); it is saved either way. Use Feishu's *reply* on one of the cards and the agent is told which card you mean.
- **🔔 `[<project dir>] waiting for you`** (orange; inside herdr only) means the agent is stuck on a prompt only you can answer — a permission dialog, a choice — and will wait until you are back at the keyboard.
- The questions are JSON the agent writes (the contract is in [SKILL.md](SKILL.md)); you never write one.

**Coming back.** Say "I'm back" or type `/agent-lark off`: the agent runs `away off` — the switch only; the group and the daemon stay. When the task is over the agent asks whether to keep the group: keep ⇒ `unbind` (the group stays in Feishu, and the next time this project turns remote mode on the agent offers it back); drop ⇒ `unbind --dissolve` (the group is dissolved and forgotten). Remote mode changes the channel, not the standard: irreversible actions still need your explicit approval, and a timeout is not approval.

## 6. Day-to-day

### 6.1 Things you ask the agent for

| you say | the agent runs |
|---|---|
| "the task changed, call it X" | `rename "X"` — the group becomes `X [<project dir>]` |
| the task ends | the agent asks whether the group should stay: "keep it" ⇒ `unbind` — the group stays in Feishu, offered back next time; "drop it" ⇒ `unbind --dissolve` — dissolved in Feishu and forgotten (if Feishu refuses, the agent tells you to dissolve it by hand; the record is gone either way) |
| "use group oc_xxxxxxxx" (one you created, or after a reinstall) | `bind --chat oc_xxxxxxxx` — the group's description is rewritten to mark it as this project's |
| "turn remote mode on / off" | `away on --name "…"` / `away off` (§5) |

### 6.2 The few commands you run yourself

```bash
agent-lark status            # credentials (which layer, never the value), herdr, daemon, every project's group
agent-lark daemon --status   # daemon: pid …  connected true  connection connected  pending questions 0  bound projects 1  started …
                             # media: ttl 7 days, … MB in … files (as of last sweep …)
agent-lark daemon --stop     # before an upgrade (§9); refused while a question is pending — --stop --force cancels it and stops
```

`agent-lark --help` lists everything else; those are the agent's commands (SKILL.md). The daemon does not have to be stopped for a new task, a new group or a context reset; it serves every project at once. **Removing the Feishu app**: an app created by `setup` is a real custom app in your tenant — first *disable* it in the Feishu admin console (workspace admin → app management), then delete it in the developer console, then `agent-lark setup --reset` (or `--reset --reuse`) when you switch to another one.

## 7. When something goes wrong

- **No card arrives, the agent reports exit 3.** The daemon is not running or cannot reach Feishu: `agent-lark daemon --status` shows the last error (wrong credentials, no network); the daemon keeps retrying by itself, so fix the cause and let the agent try again. Not running at all ⇒ the agent starts it; you can too: `agent-lark daemon --detach`.
- **The card says "Read 0/0".** That is Feishu's read counter for bot messages, not a delivery status. The signs that count: an answered question turns green, and your own message gets a `Get` reaction once it reached the agent.
- **Your message got a "Not delivered" receipt card.** The reason is on the card: no herdr on that machine, the agent's pane is gone, or the agent is stuck on a prompt only you can answer. Turning remote mode on again from the agent records its pane afresh; for the last case, handle the prompt when you are back.
- **Voice notes are saved but not transcribed.** Transcription needs the `speech_to_text:speech` scope **and a paid Feishu tenant**; on a free / personal tenant Feishu refuses (HTTP 400, code 99991400) even with the scope granted. Type instead. Keep a voice note under a minute.
- **The QR code expired.** Run `setup` again (by hand, or ask the agent again).
- **A reused app sends nothing / creating the group fails with a permission error.** Its scopes, event and callback are not enabled, or no version was published: §4.3, then try again.
- **The agent handed you a `setup --reuse` command.** You are outside herdr: run it in a terminal window of your own, not inside the agent session (it needs a real terminal for the secret), then tell the agent.
- **An old group keeps being offered.** It is offered as long as it exists in Feishu: tell the agent to create a new one and drop the old one (`unbind --dissolve` while it is bound), or dissolve it in Feishu yourself — the daemon forgets groups that are gone (once a day, and whenever it looks for groups to offer back). A group the app cannot dissolve (it is not the owner) is left for you to dissolve by hand.

Every exit code with its stderr text and what the agent is told to do: [references/failures.md](references/failures.md).

## 8. Security and limits

- Credentials live in the OS keychain or a `0600` file; the App Secret is typed by you in an interactive `setup` and never passes through the agent, argv, a file, or any output (§4.4).
- **Anyone in the project's group can drive your agent**: a tap answers, a message is an instruction (with herdr). Groups are created with only you in them; keep them that way. The channel does not filter content.
- Content travels through Feishu's servers — questions describe your project, photos and files are downloaded from Feishu — and the group's description carries your project's absolute path (that is how a group is found again). Do not put secrets in a question.
- The agent can only send files from inside the project, the daemon's media directory or the temp directory; the daemon log records ids and events, never message text.
- One pending question per project at a time; one custom app serves one tenant, and a machine stores one set of credentials.
- Card size caps, the 60-character task name and what the phone shows: [references/message-spec.md](references/message-spec.md). Files, the media directory and its 7-day retention, the per-project state file: [references/daemon.md](references/daemon.md).
- Linux and Windows have unit-test coverage only, no end-to-end run in a real environment (§2).

## 9. Upgrading

Versions are git tags `agent-lark/vX.Y.Z`; what changed is in [CHANGELOG.md](../../CHANGELOG.md). An install is a snapshot of the repository; `npx skills update` refreshes it (`-g` for global installs, `-p` for the current project). To stay on a release, install with the tag as git ref: `npx skills add 'yezhoujie/agent-remote-communication-skills#agent-lark/v0.1.2' --skill agent-lark`.

On a machine that runs the daemon: 1. `agent-lark daemon --stop` with the CLI you have now (refused while a question is pending — wait, or `--stop --force`; if the files were already replaced and the old daemon does not answer, `kill -TERM <pid>`, the pid is in `~/.agent-lark/daemon.pid`). 2. Update the files. 3. `agent-lark daemon --detach`. Credentials, group bindings and the per-project switch all carry over; then say "turn remote mode on" so the agent records its pane again.

## 10. Make it stick: the rule for your agent

The skill only provides the calls and never decides *when* to use them. Left alone, an agent uses agent-lark only when it happens to remember it exists. The trigger policy belongs in the agent's **standing instructions**, and it has to cover four moments: at session start, read the project's `state.json` (`away: true` ⇒ you are away) · when you leave, the agent runs `away on` while you are still there and asks you about old groups · while you are away, every decision becomes a question card, `notify` only for major events, never for progress · when you are back, `away off`; when the task is over, the agent asks whether to keep the group, then `unbind` or `unbind --dissolve`.

A ready-made rule that does exactly this ships with the skill — [`examples/remote-mode-rule.md`](examples/remote-mode-rule.md) (English) and [`examples/remote-mode-rule.zh-CN.md`](examples/remote-mode-rule.zh-CN.md) (Chinese); it also covers teams of agent sessions. For Claude Code, rules in `~/.claude/rules/` are injected into every session:

```bash
cp ~/.claude/skills/agent-lark/examples/remote-mode-rule.md ~/.claude/rules/agent-lark-remote-mode.md
```

For other agents, put it wherever that agent loads its standing instructions; adjust the `<skill dir>` path at the top and the trigger phrases to your own habits. **Both skills installed?** They know nothing about each other: keep one rule in force per machine (or per project) and let it name the CLI it calls; the two daemons may run side by side.
