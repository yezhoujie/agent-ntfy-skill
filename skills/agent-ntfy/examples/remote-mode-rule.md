# Remote mode (agent-ntfy): while the human is away, decisions go to the phone

> An example of an **always-loaded rule for the agent**. The skill itself only provides the calls
> (`ask` / `notify` / `away`) and never decides when to use them; a rule like this one does. Claude Code
> users: copy this file into `~/.claude/rules/` (rules there are injected into every session). Other
> agents: put it wherever your agent loads its standing instructions. Chinese version: `remote-mode-rule.zh-CN.md`.
>
> Below, `$AN` = `python3 <skill dir>/scripts/agent_ntfy.py`; with a global Claude Code install `<skill dir>` is
> `~/.claude/skills/agent-ntfy`. How to write a question card is in SKILL.md.

## Where the state lives: `<project root>/.agent-ntfy/state.json` (written by the CLI; you only read it)
- **At session start, and after your context was cleared or reset**, run `$AN away status --json` first: `away: true` ⇒ this project is already in remote mode, follow "While in remote mode"; file absent or `false` ⇒ normal terminal interaction.
- Fields: `away` (the switch) · `slot` (the slot this project currently leases, `null` = none yet) · `confirmed` (whether that slot passed the reachability check) · `target` (who wrote it). **The topic name is never in it.**

## On / off (the human's words are the switch)
- **On**: "enable remote mode", "I'm leaving, send it to my phone", "switch to phone", "remote on". The human is still at the keyboard right now — **do three things immediately** (once they are gone, nobody can do the two taps on the phone):
  1. **From your own terminal pane**, run `$AN away on` (one-stop: starts the daemon if none answers — in a herdr pane when inside herdr, detached otherwise — makes sure a confirmed slot exists, and only then writes the switch). **Relay its stdout to the user verbatim.** Three outcomes: "remote mode is on; slot slotN is ready" ⇒ done (the slot is leased on the spot); "slotN still needs its reachability check: started it in herdr pane <id>. Tell the user: 1. 2. 3." ⇒ the user subscribes to the topic shown in that pane, presses Enter there, taps the button in the phone's notification (the topic is shown only in that pane, never in your session; when the check ends one line `[agent-ntfy] slotN is confirmed / check timed out / was interrupted` arrives in your session — a system event, not the user speaking; act on it instead of polling `slots`); "is being confirmed right now (a confirmation pane is already open)" ⇒ the user finishes in that pane; rc 3 / 4 ⇒ **not enabled**, act on stderr (outside herdr with an unconfirmed slot ⇒ the user runs `$AN confirm-sub <slot>` in their own terminal; all slots leased ⇒ relay the occupancy on stderr and let the user decide: they turn remote mode off in one of those projects themselves, or you `$AN add-slot` and run `away on` again; **never `release` another project's slot** — the daemon refuses it anyway).
  2. `$AN away status --json` and check `away: true` (it also records your pane as the injection target — **run it only from your own pane**). Confirmation is asynchronous: look at `confirmed` again after the user has tapped the button.
  3. Report the stdout verdict in one sentence.
- **Off**: "disable remote mode", "I'm back", "remote off" ⇒ see "Turning it off".

## While in remote mode
- **Every moment you would otherwise ask the user a question, or need their confirmation or authorization** ⇒ `$AN ask`; do not wait in the terminal.
- `ask` blocks until the human answers. If your shell tool has a time limit (Claude Code's Bash tool: 10 minutes), **run it in the background and redirect stdout / stderr to files** — a killed foreground `ask` is treated by the daemon as cancelled and the card is voided; read the files for the reply and exit code when the background job finishes. **Only one `ask` in flight at a time** (a second one on the same slot exits 4).
- Exit codes: 0 act on the reply; 1 fix the JSON and resend; 2 timeout ⇒ for reversible work continue with the recommended option and note "unconfirmed", for irreversible work stop and wait; 3 channel failure ⇒ start the daemon and resend once, still 3 ⇒ stop; 4 a human is needed (unconfirmed slot / all leased / busy) ⇒ stop and wait for the user at the terminal.
- **Messages from the phone are injected into your session with the prefix `[agent-ntfy remote] `** (**inside herdr only**; outside herdr there is no injection — a message the user sends on their own gets a "no pane registered" receipt, while replies to `ask` still return to the call); treat them as user input. Anything the user sends afterwards counts first as the reply to the pending question.
- **`$AN notify` (one-way: JSON `{title, body, lang}` on stdin, non-blocking, no button, allowed while a question is pending) is used in exactly two situations**: ① the user asked a question from the phone that only needs an answer ("how is it going?") — put the answer in the body; ② **a major event the user must know about that needs no decision**: the task is finished / an error or exception occurred / the task cannot continue (including stopping after `ask` exited 3 or 4). Everything else — progress, intermediate results, asides — **is never sent**: each one rings the phone, and ntfy.sh allows 250 messages per day per IP. Anything that needs a decision always goes through `ask`; never substitute `notify`.
- Remote mode changes the channel, **not the standard**: irreversible actions still need explicit approval; a timeout is not approval.
- A rendered card is capped at 3584 bytes: one long commit message **per card**, never several in one; over the limit the CLI refuses to send (rc 1) — fix the JSON and resend.
- When the user should verify a change (layout, wording), do not send a separate verification card — fold it into the next card you have to send anyway ("also check X on this card"). Saves quota.
- The user answers directly in the terminal (no `[agent-ntfy remote] ` prefix) while an `ask` is still pending on the phone ⇒ stop that background job first (in Claude Code: TaskStop; the card turns "cancelled"), then act on the terminal answer. Do not wait on both.

## When several agent sessions work as a team (a lead session dispatching others)
- **Only the session that talks to the human (the lead) holds remote mode**: it leases the slot, sends `ask` / `notify`, reads `state.json`. The other sessions keep reporting to the lead as before; they lease no slot and never call `ask`.
- When another session needs the human's authorization or decision ⇒ the lead asks via `ask` and relays the answer through whatever channel the team already uses.
- Phone messages are injected into the lead's session (the pane recorded in the lease), with the `[agent-ntfy remote] ` prefix ⇒ treat as user input.
- `state.json` is the truth; after the lead's context is reset, re-read it as in the first section. The team's own status file needs one line ("remote mode: see state.json"), not a second copy.
- If the team has an "autopilot / no need to ask for each item" authorization, it is orthogonal to remote mode: the former decides what need not be asked, the latter decides which channel the things that must be asked go through.

## Turning it off and wrapping up (skipping a step raises no error)
- Turning off: make sure no background `ask` is pending (if one is ⇒ wait for it or stop it; the card turns "cancelled") → `$AN away off` (it also releases this project's slot and clears `slot` in the file; leave the daemon running).
- At the end of a task, `release` as well; if remote mode is still on (the user has not returned), leave the switch on.
- Subscribing / unsubscribing / tapping buttons on the phone are the human's actions; the agent cannot do them.

## Never
- The topic name is a password: never write it into any file, report or message (the only way to see it is `$AN confirm-sub <slot> --show-topic`, run in the user's own terminal).
- Do not keep the daemon alive from your own background shell (use `--detach`, or a separate herdr pane); do not edit `state.json` by hand — only through `away` / `ask` / `release`.
