---
name: herdr-lark
description: Reach the agent session already running in a herdr pane from Feishu/Lark. Renders a decision into a card with buttons, pushes it to the human's phone, and blocks until the verdict comes back on stdout. Messages, images and voice notes the human sends from the phone are injected into that same pane as user input. Also mirrors terminal replies into the group, sends screenshots and files, and pushes when the agent is stuck. One project, one Feishu group.
license: MIT
compatibility: "Node >= 20.12. Needs herdr (to inject back into the terminal) and a Feishu/Lark custom app, created by scanning a QR code — no workspace-admin approval. Only exercised on macOS."
---

# herdr-lark

A channel, not a policy. It renders one question of yours into a Feishu card, pushes it to the human's
phone, blocks until they answer, and prints the answer verbatim on stdout. **When to ask a human is your
(or your caller's) decision — this skill defines no triggers.**

What makes it different from a chat bridge: the answer lands back in **the session that asked**, the one
already running in a herdr pane with all of its context, rather than starting a new one.

Every command below is `dist/cli.mjs` in this skill's directory — a single self-contained file, no
`npm install` and no build step. If `herdr-lark` is not on the human's PATH yet:

```bash
ln -sf "$PWD/dist/cli.mjs" ~/.local/bin/herdr-lark
```

## Before anything works

The human runs these two, once per machine and once per project:

```bash
herdr-lark setup                        # QR code in the terminal; they scan it with Feishu
cd <project> && herdr-lark away on      # starts the daemon, creates this project's group
```

`away on` is the single entry point: it checks credentials, starts the resident daemon, creates (or
reuses) a Feishu group for this project, and only then flips the switch. If any step fails it leaves
the switch off rather than half-on.

A project is the git toplevel (the cwd outside a repo). Calling `ask` or `notify` from a project that
was never bound exits 4 — relay stderr and have the human run `away on` there.

## Ask a question

Feed one JSON object on stdin through a quoted heredoc — the fields contain quotes and line breaks, and
argv would mangle them. **The call blocks** until the human answers, it times out, or the channel fails.

```bash
ANSWER=$(herdr-lark ask <<'JSON'
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

### The field contract

| field | what to write |
|---|---|
| `title` | One line. The phone's notification shade shows the title and a line or two of body, so put the hook here |
| `doing` | One sentence: which task this is |
| `description` | Background for someone who has seen none of the work: why you got here, what is involved, jargon explained on the spot |
| `blocker` | Exactly what is blocked |
| `options[]` | 2 to 5 items of `{id, label, consequence}`; `consequence` states the real outcome and its cost, not a code name |
| `options[].danger` | Optional. Irreversible or high-cost → red button behind a native confirm dialog. **The recommendation may never be a danger option** — validation refuses it outright |
| `recommend` | The `id` of one option |
| `reasoning` | Why you lean that way **plus the strongest objection** |
| `question` | One question answerable in one sentence |
| `lang` | Optional, `zh` or `en`: the language of the fixed wording (section labels, hints). Pass the language you reply to the user in |

Write the content fields in whatever language you work in; only `lang` controls the wrapper. Validation
runs locally **before anything is sent** and reports every problem at once (exit 1, nothing sent).

### What comes back

- Exit 0: stdout is the reply, verbatim, plus a trailing newline.
- **A button tap returns that option's `label` text**, not its `id`; free typing returns exactly what the
  human typed. Match on the label, and be ready for anything else.
- The card is rewritten in place to `✅ … Answered` with the reply on top. **Buttons lock on the first
  tap** — the closed card rides back on the tap's own callback, so a second tap is not possible.
- Default wait is 12 hours (`--timeout <seconds>`). Feishu keeps history indefinitely, so longer works.
- **One pending question per project.** A second `ask` exits 4.

### ⚠️ Mind your own tool timeout

If your harness kills `ask` before it returns (most shell tools cap a command at minutes), the daemon
cancels the card and the human finds a dead question. Either set `--timeout` at or below your harness
limit and treat exit 2 as a normal outcome, or:

**Run `ask` as a background job of your harness** (in Claude Code, the Bash tool's `run_in_background`),
so it wakes you when the answer lands. A bare `nohup … &` is invisible to the harness: the answer lands
silently in a file, nobody tells you, and the human thinks you got it while you are still waiting.

## Mirror every reply: `say`

While remote mode is on, the human cannot see the terminal. **Every reply you write there must also be
`say`-ed**, or the channel is one-way: they can send to you but never see your answers.

```bash
herdr-lark say --title "One line that carries information" <<'EOF'
The reply, in markdown. Tables, lists and fenced code blocks all render.
EOF
```

- **Verbatim. Do not condense for the phone.** The human cannot verify what you cut and is left guessing
  whether the terminal held more. If it is too long, write shorter in the terminal — never ship them an
  abridged copy.
- Cards use Feishu card JSON 2.0's `markdown` component, so there is no formatting reason to rewrite.
- `--title` is *added*, not substituted: the phone's notification shade shows only the title, so make
  that line carry information — never "reply".

## Notify and files

```bash
herdr-lark notify <<'JSON'
{"title": "Tests green, starting the migration", "body": "All three CI runners pass.\n\nNext: **schema migration** on staging, about 10 minutes."}
JSON

herdr-lark send-file ./shot.png --caption "Current layout"
```

`notify` is for things that matter but need no answer — finished, crashed, blocked and giving up. Not a
progress bar: every card buzzes a phone. Day-to-day replies go through `say`; decisions always through `ask`.

`send-file` only reads files inside the calling project, the daemon's media directory, or a temp dir
(checked after `realpath`, so symlinks cannot escape). Do not send a card just to have a change verified
— fold it into the next real question.

## Messages from the phone

Anything the human sends in the group is injected into that project's pane as
`[herdr-lark remote] <their text>`. The prefix is protocol; the rest is the user speaking — **treat it
exactly like input typed at the keyboard**.

Images and files are downloaded locally first and their paths appended, so you can open them. Voice notes
are transcribed; if transcription fails you get a plain sentence saying so, never an unreadable
`<audio/>` placeholder.

**Seeing the prefix means the human is on their phone**: answer in the terminal as usual, and `say` the
same answer so it reaches them.

## Exit codes

| rc | meaning | sent? | what to do |
|---|---|---|---|
| 0 | Reply received; stdout has it | yes | continue |
| 1 | Input rejected; stderr lists every problem | **no** | fix the JSON and call again |
| 2 | No reply within the timeout | yes | reversible work: proceed with the recommendation and record "not confirmed". Irreversible: stop and wait |
| 3 | Channel failure (daemon down, send failed) | see stderr | start the daemon and retry once; still 3 → stop and tell the user |
| 4 | A human must act (not bound, question already pending) | no | relay stderr, then retry |

## The daemon

One resident process owns the Feishu WebSocket; the commands above only talk to it over a local socket.

- Start: `herdr-lark daemon --detach` · Check: `--status` · Stop: `--stop`
- **Never start it as a background job of your own shell, a monitor, or a subagent.** It dies with you,
  and every message the human sends afterwards is lost silently.
- `--stop` refuses while questions are pending (they would become dead cards); `--force` overrides.

## Remote mode

`herdr-lark away on` / `off`, read with `away status --json`. While on, the daemon pushes a card when the
agent is **stuck on a prompt only the human can answer**. "Finished" is off by default — it fires at the
end of every turn and is pure noise while they are at the keyboard; `away on --idle 30` enables it for
turns that ran at least 30 minutes.

**Remote mode changes the channel, not the standard**: irreversible actions still need explicit approval,
and a timeout is not approval.

## Wrapping up

`herdr-lark unbind` when the work is done (the Feishu group stays; archiving it is the human's call).
`herdr-lark status` shows the daemon, where credentials came from, and every binding.

Chinese documentation: [docs/guide.zh-CN.md](docs/guide.zh-CN.md) · [README.zh-CN.md](README.zh-CN.md)
