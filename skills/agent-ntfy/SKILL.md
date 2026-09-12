---
name: agent-ntfy
description: Pushes a decision that needs a human to their phone via ntfy.sh and blocks until the verdict comes back on stdout; messages the human sends from the phone arrive in the agent session as instructions. For choices the agent cannot settle on its own (unaligned requirements, a real disagreement, a technology choice) while the human may be away from the keyboard. When to ask is the caller's policy — this skill provides the call, not the trigger.
---

# agent-ntfy

Send one question to the human's phone, get one answer back on stdout. The skill is a channel
only: it renders your JSON into a fixed layout with a single button, pushes it through ntfy.sh,
and returns whatever the human replies, verbatim. It never interprets content.

Every command below is `scripts/agent_ntfy.py`, relative to this skill's directory
(`python3 <skill dir>/scripts/agent_ntfy.py …`); the CLI calls itself `agent-ntfy` in its own messages,
and that always means this script. Python 3.10 or newer, standard library only; nothing to install.

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
Missing or empty fields, a wrong option count, a `recommend` that matches no id, or a rendered body
over 3584 bytes all fail validation at once, before anything is sent. Never put secrets in the payload:
the text travels in clear through a public server.

### What comes back

- Exit 0: stdout is the reply, verbatim, plus one trailing newline (`$(…)` strips it).
- A button tap returns the recommended option's `label` text, not its `id`; free typing returns
  exactly what the human typed. Match on the label, and be ready for anything else.
- The card on the phone is replaced by an "Answered" record automatically. Nothing to clean up.
- Default wait is 12 hours (`--timeout <seconds>` to change). ntfy keeps messages 12 hours, so a
  longer wait cannot help.
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

## Exit codes

| rc | meaning | sent? | what to do |
|---|---|---|---|
| 0 | Reply received; stdout has it | yes | continue |
| 1 | Input rejected; stderr lists every problem | **no** | fix the JSON and call again |
| 2 | No reply within the timeout | yes | decide yourself or ask again; a late reply still reaches you as an instruction |
| 3 | Channel failure: daemon not running, connection lost, publish failed; stderr says whether the message was sent | see stderr | start the daemon (below) or report the failure |
| 4 | A human must act on the terminal side: slot not confirmed, all slots leased, or this target already has a question waiting | no | relay stderr to the user in your own conversation, then retry |
| 130 | The `ask` client itself was interrupted (Ctrl-C); the daemon is unaffected, do not restart it | see stderr | stderr says sent → same as rc 2 (a late reply arrives as an instruction); NOT sent → call again |

Stderr text per case, what to tell the user for each, and how to read a validation report:
[references/failures.md](references/failures.md).

## Several replies, contradicting each other

Taps are not deduplicated and there is no confirmation step on the phone. The human may tap twice,
tap then type, or correct themselves. Only the first message closes the question (exit 0); each later
one arrives as a separate instruction in your session. **Take the last one as the verdict** unless you
have a reason not to. The channel does not merge, filter, or judge.

## The daemon

One resident process subscribes to ntfy for every question; `ask` only talks to it over a local socket.
If it is not running, `ask` exits 3 without sending, and stderr prints the start commands.

You may start it yourself, but **never from your own shell as a background job, a Monitor, or a
subagent**: it dies with you, and every message the human sends afterwards is lost silently.

- Inside herdr: split a pane (`herdr pane split --current --direction right --cwd "$PWD" --no-focus`)
  and run `python3 <skill dir>/scripts/agent_ntfy.py daemon` in it (`herdr pane run <new pane id> "…"`).
- Anywhere else: `python3 <skill dir>/scripts/agent_ntfy.py daemon --detach`.
- `daemon --status` / `daemon --stop` to inspect or stop it.

Lifecycle, environment variables, slots, and the confirmation gate: [references/daemon.md](references/daemon.md).

## Messages the human sends on their own

Anything the human types in the phone app while no question is pending is injected into your session
as a plain instruction (it is the user speaking; there is no prefix or envelope). You do nothing to
receive it. If delivery is impossible, the human gets a receipt on the phone, not you.

## Remote mode and the per-project state file

Whether to route decisions to the phone is the caller's policy (a rule in the user's own config, not
this skill). The switch and the current slot live in `<project root>/.agent-ntfy/state.json`
(project root = the git toplevel, else the cwd), written by `away on|off`
and refreshed by `ask` / `confirm-sub` / `release`. Read it with `away status --json`:

```json
{"away": true, "slot": "slot2", "confirmed": true, "target": "wG:p1", "updated": "2026-09-12T21:04:11+08:00"}
```

`away: true` means the human is away and expects decisions on the phone. `slot` is the lease this
project currently holds (`null` until the first `ask`); `confirmed: false` means that slot still needs
`confirm-sub`. The file never contains the topic name. If the directory is absent, the project has
never enabled remote mode and nothing is written.

## Housekeeping

- One target (your pane inside herdr; otherwise `AGENT_NTFY_TARGET` or host + session id) holds one
  slot, and one question at a time. Leases never expire on their own.
- When your task ends, run `release` (no argument releases your own slot). `slots` shows the pool.
- `release` also clears `slot` in the state file; `away off` is the human's call, not yours.

## References

- [references/message-spec.md](references/message-spec.md): writing the question, byte budget, rendered result, bad vs good.
- [references/failures.md](references/failures.md): every exit code with its stderr, what to relay to the user, validation reports.
- [references/daemon.md](references/daemon.md): daemon lifecycle, the reachability gate, slots and leases, environment variables.
