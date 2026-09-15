# Remote mode (agent-lark): while the human is away, decisions go to the phone

> An example of an **always-loaded rule for the agent**. The skill itself only provides the calls
> (`ask` / `notify` / `send-file` / `away` / `rename` / `unbind`) and never decides when to use them; a rule
> like this one does. Claude Code users: copy this file into `~/.claude/rules/` (rules there are injected
> into every session). Other agents: put it wherever your agent loads its standing instructions. Chinese
> version: `remote-mode-rule.zh-CN.md`.
>
> Below, `$AL` = `node <skill dir>/dist/cli.mjs`; with a global Claude Code install `<skill dir>` is
> `~/.claude/skills/agent-lark`. How to write a question card is in SKILL.md.

## Where the state lives: `<project root>/.agent-lark/state.json` (written by the CLI; you only read it)
- **At session start, and after your context was cleared or reset**, run `$AL away status --json` first: `away: true` ⇒ this project is already in remote mode, follow "While in remote mode"; file absent or `false` ⇒ normal terminal interaction.
- Fields: `away` (the switch) · `chatId` (the Feishu group this project is bound to, `null` = none) · `target` (the project root) · `updated`. **No credential is ever in it**, and the pane phone messages are injected into is not either — the daemon keeps that.

## On / off (the human's words are the switch)
- **On**: "enable remote mode", "I'm leaving, send it to my phone", "switch to phone", "remote on" — and `/agent-lark on` typed to you means the same (SKILL.md "Invoked with an argument"). The human is still at the keyboard right now — **do three things immediately** (a group can only be chosen or a QR code scanned while they are here):
  1. **From your own terminal pane**, run `$AL away on --name "<task>"` — one-stop: starts the daemon if none answers, waits for it to reach Feishu, binds this project to a group named `<task> [<dir>]`, and only then writes the switch. **Relay its stdout to the user verbatim.** Outcomes:
     - rc 0 ⇒ done. The group line says which group (`Created Feishu group "…"` / `Took back Feishu group "…"` / `Connected to Feishu group "…"`); the switch line `Remote mode is on: …` is the last line inside herdr; outside herdr one more line follows it, `Not inside herdr: …` (phone messages are not injected, no stuck alert).
     - rc 4 `No Feishu app credentials yet` ⇒ **ask the user first** whether to scan a QR code for a new app or reuse an app they already have (they know its App ID and App Secret). QR code ⇒ run `$AL setup </dev/null` for them in the background and hand over the URL line it prints under the code (or render it into a PNG and open it). Reuse ⇒ run `$AL setup --reuse`: inside herdr it opens a pane below yours where the user types the App ID and the App Secret (the secret never passes through you) and, when they are done, one line prefixed `[agent-lark] setup:` arrives in your session — wait for it; outside herdr it exits 4 and prints the exact command on stderr — give that command to the user to run in their own terminal and let them tell you when it is done. The user must run it in a terminal window of their own (Terminal, iTerm, …). Never suggest running it inside this session — a `!`-prefixed command, a shell tool, a background job: none of them has a TTY, and the CLI refuses without one. Then run `away on` again. (The same flow is `/agent-lark setup`; SKILL.md "Invoked with an argument".)
     - rc 4 `this project has no live group, but N earlier group(s) could be taken back` ⇒ relay the list on stderr (one line per group: name, when it was let go of, id) and **ask the user which one to rename and reuse, or whether to create a new one** — never pick for them; rerun with `--reuse <chatId>` or `--new`.
     - rc 3 ⇒ **not enabled**, nothing written. Run `$AL daemon --status`; if the daemon is not running, `$AL daemon --detach` and try `away on` once more; still 3 (Feishu unreachable, `daemon is up but not connected to Feishu: …`) ⇒ stop and tell the user what stderr says.
  2. `$AL away status --json` and check `away: true` (`away on` also recorded your pane as the injection target — **run it only from your own pane**).
  3. Report the stdout verdict in one sentence.
- **Off**: "disable remote mode", "I'm back", "remote off", or `/agent-lark off` ⇒ see "Turning it off and wrapping up".

## While in remote mode
- **Every moment you would otherwise ask the user a question, or need their confirmation or authorization** ⇒ `$AL ask`; do not wait in the terminal.
- `ask` blocks until the human answers. If your shell tool has a time limit (Claude Code's Bash tool: 10 minutes), **run it in the background and redirect stdout / stderr to files** — a killed foreground `ask` is treated by the daemon as cancelled and the card is voided; read the files for the reply and exit code when the background job finishes. **Only one `ask` in flight per project** (a second one exits 4).
- Exit codes: 0 act on the reply; 1 fix the JSON and resend; 2 timeout ⇒ for reversible work continue with the recommended option and note "unconfirmed", for irreversible work stop and wait; 3 channel failure ⇒ `$AL daemon --status`, start it with `--detach` if it is down, resend once, still 3 ⇒ stop; 4 a human is needed (not bound / question already pending) ⇒ stop and wait for the user at the terminal.
- **`--urgent` only for an irreversible action or a short timeout** (it rings the owner in-app; flag every question and none is urgent any more). An irreversible option is marked `"danger": true` (red button, confirm dialog) and is never the recommendation — validation refuses that.
- **`"select": "multi"` only when the honest answer may be several options at once** ("which of these checks should run"); a yes / no or a pick-one question stays single choice — one tap instead of several.
- **Messages from the phone are injected into your session with the prefix `[agent-lark remote] `** (**inside herdr only**; outside herdr there is no injection — a message the user sends on their own gets a "Not delivered" receipt card, while replies to `ask` still return to the call); treat them as user input. Photos and files arrive with `[saved: <path>]` lines (open them from there); a voice note arrives as its transcript.
- **Which question a phone message answers**: while an `ask` of yours is pending, **anything** the user sends counts as its reply, even a message written as a Feishu reply to some other card. With nothing pending, a Feishu reply to one of your cards arrives with `(reply to: "<that card's title>")` on its first line — that is what "yes, do that" refers to; a message quoting nothing is a plain instruction. A late tap on a closed card arrives as `(follow-up) I pick <label>`; take the latest message as the verdict.
- **When you see the prefix, the user is on the phone**: answer in the terminal as usual, and push the same answer with `notify`.
- **`$AL notify` (one-way: JSON `{title, body, lang}` on stdin, non-blocking, no button, allowed while a question is pending) is used in exactly two situations**: ① the user asked a question from the phone that only needs an answer ("how is it going?") — put the answer in the body; ② **a major event the user must know about that needs no decision**: the task is finished / an error or exception occurred / the task cannot continue (including stopping after `ask` exited 3 or 4). Everything else — progress, intermediate results, asides — **is never sent**: each card rings the phone. Anything that needs a decision always goes through `ask`; never substitute `notify`.
- **`$AL send-file <path> --caption "…"` saves a round trip**: when the user has to look at a layout, a diff or a build product, send the file (a screenshot, the rendered page, the artifact) instead of describing it and waiting to be asked. Only files inside the project, the daemon's media directory or the temp directory can be sent.
- When the user should verify a change (layout, wording), do not send a separate verification card — fold it into the next card you have to send anyway ("also check X on this card"), or `send-file` the thing itself.
- **The 🔔 "waiting for you" card is automatic** (inside herdr, while remote mode is on): the daemon pushes it when herdr reports your session stuck on a prompt only a human can answer. Never send one by hand, and do not `notify` about being stuck — if you can still run a command you are not stuck.
- Remote mode changes the channel, **not the standard**: irreversible actions still need explicit approval; a timeout is not approval.
- The user answers directly in the terminal (no `[agent-lark remote] ` prefix) while an `ask` is still pending on the phone ⇒ stop that background job first (in Claude Code: TaskStop; the card turns "Cancelled"), then act on the terminal answer. Do not wait on both.
- **The task changes** (the user hands you something else in the same project) ⇒ `$AL rename "<new task>"` so the group name on the phone says what this is about.

## When several agent sessions work as a team (a lead session dispatching others)
- **Only the session that talks to the human (the lead) holds remote mode**: it runs `away on`, sends `ask` / `notify` / `send-file`, reads `state.json`. The other sessions keep reporting to the lead as before; they never call `ask`.
- When another session needs the human's authorization or decision ⇒ the lead asks via `ask` and relays the answer through whatever channel the team already uses.
- Phone messages are injected into the lead's session (the pane recorded on the binding), with the `[agent-lark remote] ` prefix ⇒ treat as user input.
- `state.json` is the truth; after the lead's context is reset, re-read it as in the first section. The team's own status file needs one line ("remote mode: see state.json"), not a second copy.
- If the team has an "autopilot / no need to ask for each item" authorization, it is orthogonal to remote mode: the former decides what need not be asked, the latter decides which channel the things that must be asked go through.

## Turning it off and wrapping up (skipping a step raises no error)
- Turning off: make sure no background `ask` is pending (if one is ⇒ wait for it or stop it; the card turns "Cancelled") → `$AL away off` (the switch only; the group and the binding stay, the daemon keeps running).
- **At the end of a task, remind the user and run `$AL unbind`**: the group is theirs (it stays in Feishu; archiving it is their call), and the next `away on` in this directory will offer it back for renaming. If remote mode is still on (the user has not returned), leave the switch on and do the reminder through `notify`.
- Subscribing, tapping buttons, scanning the QR code and anything else on the phone are the human's actions; the agent cannot do them.

## Never
- Never run `setup` without asking first, and never choose the path for the user (it creates or binds a Feishu app under their account): ask whether to scan a QR code for a new app or reuse an app they already have; once they have chosen you may run `setup` (QR code: hand them the URL line, or render it into a PNG yourself and open it) or `setup --reuse` (herdr pane, or the command for them) for them. The App Secret is typed by the user themselves in the interactive `setup`; it never passes through you, and must not appear in argv, a file you write, or any output.
- Do not keep the daemon alive from your own background shell (use `daemon --detach`); do not `daemon --stop --force` while a question is pending; do not edit `state.json` or `bindings.json` by hand — only through `away` / `bind` / `unbind` / `rename`.
- Do not `bind --chat` a group that belongs to another project; do not reuse a group the user has not chosen.

## Both skills installed (agent-ntfy and agent-lark)
The two skills know nothing about each other, and neither decides which one a decision goes to. Keep **one** remote-mode rule in force per machine (or per project) and let it name the CLI it calls: this file for agent-lark, `agent-ntfy`'s `examples/remote-mode-rule.md` for ntfy. To route by project instead, keep both rules but open each with one line such as "This rule applies only when the project has `.agent-lark/state.json`" / "… `.agent-ntfy/state.json`" and let `away status --json` of the matching CLI decide. The daemons may run side by side; they share nothing.
