---
name: agent-ntfy
description: Pushes a decision that needs a human to their phone via ntfy.sh and blocks until the verdict comes back on stdout; messages the human sends from the phone arrive in the agent session as instructions. For choices the agent cannot settle on its own (unaligned requirements, a real disagreement, a technology choice) while the human may be away from the keyboard. When to ask is the caller's policy — this skill provides the call, not the trigger. It can also push one-way notifications (notify) to the same phone.
license: MIT
compatibility: "Python >= 3.10, standard library only. Needs ntfy.sh (or a self-hosted ntfy) reachable. Phone -> agent injection needs herdr. Linux/Windows: not end-to-end tested."
---

# agent-ntfy

Send one question to the human's phone, get one answer back on stdout. The skill is a channel
only: it renders your JSON into a fixed layout with a single button, pushes it through ntfy.sh,
and returns whatever the human replies, verbatim. It never interprets content. A second command,
`notify`, pushes a one-way progress note to the same phone and returns at once.

Every command below is `scripts/agent_ntfy.py`, relative to this skill's directory
(`python3 <skill dir>/scripts/agent_ntfy.py …`); the CLI calls itself `agent-ntfy` in its own messages,
and that always means this script. Python 3.10 or newer, standard library only; nothing to install.
The shell forms in this file are POSIX (`$(…)`, quoted heredocs): on Windows run them from Git Bash
(or WSL, where the skill simply runs as Linux); the Windows interpreter is `python`, as the test
workflow runs it.

## When to use

Any point where your workflow needs a human decision you cannot make yourself and the human may be
away from the terminal. Whether and when to ask is your (or your caller's) policy: this skill defines
no triggers, installs no hooks, and does not replace your CLI's own "ask the user" mechanism.

## Ask a question

Feed one JSON object on stdin through a quoted heredoc (the fields contain quotes and line breaks;
argv would mangle them). The call blocks until the human answers, the wait times out, or the channel fails.

```bash
ANSWER=$(python3 <skill dir>/scripts/agent_ntfy.py ask <<'JSON'
{
  "title":       "Keep or delete the scratch directory when no checkout exists",
  "doing":       "Letting the requirements assistant run before the project code is checked out",
  "description": "Until now the assistant required a local code directory. That restriction is lifted, so we must decide where its temporary subprocess runs when there is no checkout.",
  "blocker":     "With no code directory there is no natural working directory for that subprocess.",
  "options": [
    {"id": "keep", "label": "Keep a fixed directory", "consequence": "One directory per project. Leaves a scene to inspect after failures; the cost is directories piling up with nobody cleaning them"},
    {"id": "temp", "label": "Delete after use",       "consequence": "Clean, but nothing is left to inspect after a crash; debugging relies on logs alone"}
  ],
  "recommend": "keep",
  "reasoning": "Keep a fixed directory: users on this path are the ones most likely to have a broken setup, so a scene is worth having. Strongest objection: disk clutter accumulates.",
  "question":  "Keep a fixed directory, or delete after use?",
  "lang":      "en"
}
JSON
)
rc=$?
```

### JSON contract: eight required fields, one optional

| field | what to write |
|---|---|
| `title` | One line. The notification preview shows the title and at most a line or two of body, so put the hook here |
| `doing` | One sentence: which task this is |
| `description` | Background for someone who has seen none of the work: why you got here, what is involved, jargon explained on the spot |
| `blocker` | Exactly what is blocked |
| `options[]` | 2 to 5 items of `{id, label, consequence}`; `consequence` states the real outcome, not a code name |
| `recommend` | The `id` of one option |
| `reasoning` | Why you lean that way **plus the strongest objection** |
| `question` | One question answerable in one sentence |
| `lang` | Optional, `zh` or `en`: the language of the fixed wording (section labels, button, hints). **Pass the language you are configured to reply to the user in: `zh` if you reply in Chinese, otherwise `en`.** Invalid values are rejected, never silently defaulted |

The content fields are written in whatever language you work in; only `lang` controls the wrapper.
For every other command (`away`, `confirm-sub`, `slots`, `daemon`, …) the wording language is resolved once at
process start: `--lang zh|en` (a top-level option, before the subcommand) > `AGENT_NTFY_LANG` > the system
locale (a Chinese `LC_ALL` / `LC_MESSAGES` / `LANG`, or a Chinese Windows locale, gives `zh`) > `en`. Panes and
daemons the CLI starts for you get that resolved language passed along, so pass `--lang` (or set the
variable) once if the user's shell locale is not what they read in.
Missing or empty fields, a wrong option count, a `recommend` that matches no id, or a rendered body
over 3584 bytes all fail validation at once, before anything is sent. Never put secrets in the payload:
the text travels in clear through a public server.

### What comes back

- Exit 0: stdout is the reply, verbatim, plus one trailing newline (`$(…)` strips it).
- A button tap returns the recommended option's `label` text, not its `id`; free typing returns
  exactly what the human typed. Match on the label, and be ready for anything else.
- The card on the phone is replaced by an "Answered" record automatically. Nothing to clean up.
- Default wait is 12 hours (`--timeout <seconds>` to change). ntfy keeps messages 12 hours, so a
  longer wait cannot help. With a very short `--timeout` (under roughly 90 s) the card on the phone
  may never flip to "Timed out": the exit code and everything after it are unaffected.
- **Mind your own tool timeout.** If your harness kills `ask` before it returns (most shell tools cap a
  command at minutes), the daemon cancels the card (`⚠️ Cancelled`, button gone), you get no exit code,
  and the human finds a dead question. Either set `--timeout` at or below your harness limit and treat
  rc 2 as a normal outcome, or run `ask` as a background job of your session with stdout and stderr
  redirected to files and read them when it ends. Details: [references/failures.md](references/failures.md) §3.

## Write for someone who saw none of the work

1. **The reader has no context.** Say what the task is, why it reached this point, and what each
   option actually does. Internal code names, pane ids, and branch names explain nothing.
2. **Never just throw options at them.** `reasoning` is mandatory for that reason: state your lean
   and the strongest argument against it.
3. **The only button is your recommendation.** Do not recommend an irreversible or high-cost option;
   list it so the human has to type it, and confirm a high-stakes verdict with a second `ask`.

Field-by-field guidance, the byte budget, and a worked bad/good pair: [references/message-spec.md](references/message-spec.md).

## Notify: one-way progress

`notify` pushes a card with a title and a body, no button, and returns as soon as ntfy has accepted it.
Use it to report progress, a finished step, or anything the human should see but need not answer.

```bash
python3 <skill dir>/scripts/agent_ntfy.py notify <<'JSON'
{
  "title": "Tests green, starting the migration",
  "body":  "All tests pass on the three CI runners.\n\nNext: **schema migration** on the staging database (about 10 minutes). I will notify again when it is done.",
  "lang":  "en"
}
JSON
```

- `title` and `body` are required and must be non-empty; `lang` is optional and works as for `ask`.
  `body` may use Markdown (bold, lists, `---` rules); rendered body ≤ 4096 bytes, title ≤ 960 bytes.
  The body is your own Markdown, sent as is: the Android ntfy app renders an ordered list (`1. …`) as
  bullets without the numbers, so when the numbers matter write them escaped, `1\. …`, `2\. …`.
- Exit codes: **0** sent (stdout: `notification sent on slotN (if the user replies, it arrives as an
  instruction)`) · **1** input rejected, nothing sent · **3** channel failure (daemon not running,
  publish failed) · **4** a human must act (slot not confirmed, no usable slot). There is no rc 2:
  nothing is waited for.
- It uses the same slot as `ask` and follows the same lease rules; it is allowed **while a question of
  yours is still pending**, so you can report progress while waiting for a verdict.
- The human may answer a notification by typing in the topic. The channel cannot tell a reply to the
  notification from a reply to a pending question: **while a question is pending, whatever the human
  sends counts as the answer to that question**; with nothing pending it reaches you as an instruction
  (next section).
- Notifications count against the ntfy.sh quota (about 250 messages per day per source IP, shared with
  questions, updates and receipts). Do not narrate every step; one note per milestone.
- Two identical calls send two cards; nothing is deduplicated.

## Exit codes

| rc | meaning | sent? | what to do |
|---|---|---|---|
| 0 | Reply received; stdout has it | yes | continue |
| 1 | Input rejected; stderr lists every problem | **no** | fix the JSON and call again |
| 2 | No reply within the timeout | yes | decide yourself or ask again; a late reply still reaches you as an instruction |
| 3 | Channel failure: daemon not running, connection lost, publish failed; stderr says whether the message was sent | see stderr | start the daemon (below) or report the failure |
| 4 | A human must act on the terminal side: slot not confirmed, no usable slot, or this project already has a question waiting | no | relay stderr to the user in your own conversation, then retry |
| 130 | The `ask` client itself was interrupted (Ctrl-C); the daemon is unaffected, do not restart it | see stderr | stderr says sent → same as rc 2 (a late reply arrives as an instruction); NOT sent → call again |

`notify` uses 0 / 1 / 3 / 4 with the same meanings and never 2. Stderr text per case, what to tell the
user for each, and how to read a validation report: [references/failures.md](references/failures.md).

## Several replies, contradicting each other

Taps are not deduplicated and there is no confirmation step on the phone. The human may tap twice,
tap then type, or correct themselves. Only the first message closes the question (exit 0); each later
one arrives as a separate instruction in your session. **Take the last one as the verdict** unless you
have a reason not to. The channel does not merge, filter, or judge.

The ntfy app has **one input box per topic**, not one per card: a typed message is "the reply to the
pending question" if there is one, and a free-standing message otherwise. Never tell the human to
"reply in that card's box"; say "send a message in this topic".

## The daemon

One resident process subscribes to ntfy for every question; `ask` and `notify` only talk to it over a
local IPC endpoint (a Unix socket, or a loopback TCP port on Windows). If it is not running, they exit 3
without sending, and stderr prints the start commands.

You may start it yourself, but **never from your own shell as a background job, a Monitor, or a
subagent**: it dies with you, and every message the human sends afterwards is lost silently.

- Inside herdr: `away on` (next section) starts it for you — and switches remote mode on, so use the
  by-hand form if you only need the daemon: split a pane
  (`herdr pane split --current --direction right --cwd "$PWD" --no-focus`) and run
  `python3 <skill dir>/scripts/agent_ntfy.py daemon` in it (`herdr pane run <new pane id> "…"`).
- Anywhere else (macOS, Linux, Windows): `python3 <skill dir>/scripts/agent_ntfy.py daemon --detach`
  (a detached process: its own session on POSIX, a background process without a console window on
  Windows, by the `DETACHED_PROCESS` flag).
- `daemon --status` prints one line ending in `transport: unix` or `transport: tcp`; `daemon --stop`
  asks it over the same endpoint to shut down (no signals involved) and waits for its files to go.

Lifecycle, environment variables, slots, and the confirmation gate: [references/daemon.md](references/daemon.md).

## Messages the human sends on their own

Anything the human types in the phone app while no question is pending is injected into your session
as an instruction, prefixed with the marker `[agent-ntfy remote] ` on the same line; the text after the
marker is the user's, unchanged. It is the user speaking, not another agent: treat it exactly like
input typed at the keyboard. You do nothing to receive it. If delivery is impossible, the human gets a
receipt on the phone, not you.

**When you see the marker, the user is on the phone**: answer in the terminal as usual, and push the
same answer with `notify` so it reaches them where they are.

## Remote mode and the per-project state file

Whether to route decisions to the phone is the caller's policy (a rule in the user's own config, not
this skill). Enabling it is one command:

```bash
python3 <skill dir>/scripts/agent_ntfy.py away on
```

It starts the daemon if needed (in a herdr pane when inside herdr, detached otherwise), makes sure a
slot the phone actually receives is available, and only then writes the state file. **Relay its stdout
to the user**; the outcomes are:

- `remote mode is on; slot slotN is ready` or `… a slot is leased automatically on the first ask …` —
  nothing to do.
- `… slot slotN still needs its reachability check: started it in herdr pane <id>. Tell the user: 1. … 2. … 3. …` —
  the topic is shown only in that pane; the user subscribes there, presses Enter, taps the button on the
  phone. Tell them **not to close that pane before pressing Enter and tapping the button** (closing cancels
  the check); on success the pane asks `Close this pane? [Y/n]` and closes itself on Enter. **When the check
  ends, one line arrives in your session with the prefix `[agent-ntfy] `** — `slotN is confirmed …`,
  `… check timed out …` or `… was interrupted …`: a system event, not a user message. You do not need to poll
  `slots`; act on that line (an `ask` may follow, or tell the user to run `confirm-sub` again). If nothing
  arrives (the user closed the pane by hand, or the injection failed), `slots` shows the truth.
- `… slot slotN is being confirmed right now (a confirmation pane is already open): …` — a pane from an
  earlier run is still open; the user finishes there.
- rc 3 (daemon did not come up, or no herdr pane could be opened) or rc 4 (outside herdr and the slot is
  unconfirmed, so the user must run `confirm-sub <slot>` in their own terminal — relay that; when their run
  finishes it prints one line for them to send back to you, `agent-ntfy: slotN is confirmed; …`, because
  without herdr nothing reaches you by itself; or every slot is leased: stderr `All slots are leased. Release
  an idle one …` with `candidate:` lines) with the reason on stderr: remote mode is **not** enabled and
  nothing is written.

The switch and the current slot live in `<project root>/.agent-ntfy/state.json` (project root = the git
toplevel, else the cwd), written by `away on|off` and refreshed by `ask` / `notify` / `confirm-sub` /
`release`. Read it with `away status --json`:

```json
{"away": true, "slot": "slot2", "confirmed": true, "target": "proj:/Users/me/work/my-project", "updated": "2026-09-12T21:04:11+08:00"}
```

`away: true` means the human is away and expects decisions on the phone; while it is set, `ask` and
`notify` use only slots that have passed `confirm-sub`. `slot` is the lease this project currently holds
(`null` until the first `ask`); `confirmed: false` means that slot still needs `confirm-sub`. The file
never contains the topic name. If the directory is absent, the project has never enabled remote mode and
nothing is written.

`away status` (with or without `--json`) checks the file against the daemon's leases and corrects it
when they disagree. Like `ask` / `notify` / `slots`, it also records the herdr pane it was run from as the
injection target for this project: **run it from your own pane**, never from a helper process elsewhere,
or the user's next phone message lands in the wrong pane.

## Housekeeping

- The lease holder is the **project** (git toplevel, else the cwd; a worktree or a submodule is its own
  project), so every pane and session in the same project shares one slot and one question at a time.
  `AGENT_NTFY_TARGET`, if set, names the holder instead. Leases never expire on their own.
- The pane the last command ran from is remembered as the injection target; `ask`, `notify`, `slots`,
  `release` (no argument) and `away on|status` refresh it.
- When your task ends, run `release` (no argument releases this project's slot). `slots` shows the pool.
- `release` also clears `slot` in the state file; `away off` is the human's call, not yours.
- After an upgrade: restart the daemon, and free leases left by an older version with `release <slot>`
  ([references/daemon.md](references/daemon.md) §4).

## References

- [references/message-spec.md](references/message-spec.md): writing the question, byte budget, rendered result, bad vs good.
- [references/failures.md](references/failures.md): every exit code with its stderr, what to relay to the user, validation reports.
- [references/daemon.md](references/daemon.md): daemon lifecycle, the reachability gate, slots and leases, environment variables.
