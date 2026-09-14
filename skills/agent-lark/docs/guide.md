# herdr-lark

**English** | [简体中文](./guide.zh-CN.md)

Reach the agent session **already running** in your herdr pane — from Feishu/Lark on your phone.

When it hits something only you can decide, it pushes a card with buttons to your phone. You tap one or type a sentence, and the answer lands back in **that same session** — the one that has been running for two hours and holds all the context. Not a fresh one.

> This is the opposite direction from bridges that *start* a new agent from a chat app. Those solve "I'm away from my desk and want the agent to do something new." This solves "I'm in the middle of work, I stepped away, the session is still running — reach me when it matters."

## Requirements

- **Node.js ≥ 20.12**
- **[herdr](https://herdr.dev)** — answers are injected back into a terminal pane through it
- **A Feishu/Lark account** — a personal one is enough. The app is created by scanning a QR code; **no workspace-admin approval needed**

## Install

`dist/cli.mjs` ships in the repo as a single self-contained file — the Feishu SDK is bundled in, so there is nothing to install and nothing to build:

```bash
npx skills add tcyufeng/herdr-lark      # or just git clone
ln -sf "$PWD/.agents/skills/herdr-lark/dist/cli.mjs" ~/.local/bin/herdr-lark
```

Building from source (only needed if you change the code): `npm install && npm run build`.

## Set up

```bash
herdr-lark setup            # prints a QR code; scan it with Feishu — the app is created on the spot
cd <your project> && herdr-lark away on
```

The confirmation page lists the permissions being requested: send and receive messages, send as the bot, receive group messages without an @-mention, create groups, upload/download resources, transcribe voice — plus the `im.message.receive_v1` event and the `card.action.trigger` callback. Missing one later? `herdr-lark setup --update` adds it to the same app.

`away on` is the single command that gets everything ready: check credentials → start the daemon in the background → create (or reuse) this project's group → flip the switch.

Already have an app? Skip the QR code. **The secret is read from the environment, never from argv** — argv is visible to every process on the machine:

```bash
HERDR_LARK_APP_ID=cli_xxx HERDR_LARK_APP_SECRET=xxx herdr-lark setup
```

## Use

Day to day you don't touch these commands. Copy [`examples/remote-mode-rule.md`](../examples/remote-mode-rule.md) into `~/.claude/rules/` and just tell your agent "I'm heading out" / "I'm back".

```bash
herdr-lark ask <<'JSON'      # push a question card, block, print the answer on stdout
{"title": "...", "doing": "...", "description": "...", "blocker": "...",
 "options": [{"id":"keep","label":"Keep the directory","consequence":"A scene to inspect after failures; the cost is clutter"},
             {"id":"wipe","label":"Wipe history too","consequence":"Unrecoverable","danger":true}],
 "recommend": "keep", "reasoning": "...", "question": "...", "lang": "en"}
JSON

herdr-lark say --title "Done" <<'EOF'    # mirror a terminal reply into the group
Body, in markdown. Tables, lists and code blocks all render.
EOF

herdr-lark notify <<'JSON'   # something important, no answer needed
{"title": "Migration finished", "body": "..."}
JSON

herdr-lark send-file shot.png --caption "Current layout"
herdr-lark status
```

Field-by-field rules, exit codes, and how to write a question worth answering: [SKILL.md](../SKILL.md).

### Three kinds of card

| | Colour | When |
|---|---|---|
| 🤔 | blue | `ask` — needs your call, has buttons, the caller is blocked on you |
| 💬 | turquoise | `say` — a terminal reply, mirrored verbatim |
| 📣 | light blue | `notify` — worth knowing, needs no reply |

An `ask` button **locks the moment you tap it**: the closed card rides back on the tap's own callback, so there is no window in which a second tap is possible. An option marked `"danger": true` gets a red button behind a native confirm dialog — and **can never be the recommendation**; validation refuses it outright.

### The other direction: phone → terminal

When no question is pending, anything you send in the group is injected into that project's herdr pane, prefixed `[herdr-lark remote] `. **Images work too** — they are downloaded locally and the path is appended to the injected text, so the agent can actually open them. **Voice messages are transcribed** through Feishu's speech-to-text.

If injection fails (the agent is stuck on a prompt only you can answer, the pane is gone), a receipt card appears in the group.

### Agent state pushes

With `away on`, you get a card when the agent is **stuck on a prompt that needs you** — nobody else can unblock it, and it will wait forever.

"Finished" is **off by default**: it fires at the end of every conversational turn, which is pure noise while you are at the keyboard. Want it? `away on --idle 30` — only turns that ran at least 30 minutes.

## One project, one group

A project is the git toplevel (the cwd outside a repo); worktrees and submodules each count as their own.

This is not cosmetics. In a single chat, an offhand message carries no clue about which project it belongs to — the only option is to guess "most recently active", and a wrong guess injects your instruction into **a different project's agent**, which will happily act on it. That is a damaging error, not a display glitch. Per-group notification muting also lands exactly where it matters when you have stepped away.

Groups are created and reused automatically, matched on the project's **absolute path** (stored in the group description). Losing the local binding file will not create a duplicate, and two worktrees with the same basename will not be confused for each other.

## Credentials

Resolution order, highest first:

1. `HERDR_LARK_APP_ID` / `HERDR_LARK_APP_SECRET`
2. env file `~/.config/herdr-lark/.env` (`HERDR_LARK_ENV_FILE` to move it)
3. **OS keychain** — macOS `security`, Linux `secret-tool`, Windows DPAPI. This is where `setup` writes
4. `~/.config/herdr-lark/credentials.json`, mode 0600; a looser mode gets a warning
5. generic `LARK_APP_ID` / `LARK_APP_SECRET`

Layer 5 is last on purpose: several Feishu tools read those names, so a machine running more than one would otherwise cross-wire.

`herdr-lark status` prints all five layers and which one actually matched — **and never prints a value**. Secrets never travel through argv either.

## Where things live

| Path | Contents |
|---|---|
| `~/.herdr-lark/daemon.sock` | local IPC |
| `~/.herdr-lark/bindings.json` | project ↔ group ↔ pane |
| `~/.herdr-lark/daemon.log` | ids and state transitions only — **never message content** |
| `~/.herdr-lark/media/` | images, files and voice notes from the phone |
| `<project root>/.herdr-lark/state.json` | remote-mode switch, group id, pane id (ships its own `.gitignore`) |

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `HERDR_LARK_HOME` | `~/.herdr-lark` | state directory |
| `HERDR_LARK_STORE` | keychain when available | `keychain` / `file` / `none` |
| `HERDR_LARK_KEYCHAIN` | `herdr-lark` | keychain service name |
| `HERDR_LARK_ENV_FILE` | `~/.config/herdr-lark/.env` | env file location |
| `HERDR_LARK_LANG` | `zh` | language of the fixed card wording (`zh` / `en`) |

## Known limits

- **Only exercised on macOS.** The Linux (`secret-tool`) and Windows (DPAPI) credential stores are written per platform but untested.
- A project holds **one pending question at a time** — a second `ask` exits 4. A typed answer cannot be tied to a specific card, so concurrency is not attempted.
- One custom Feishu app serves one tenant. A work account and a personal account each need their own app, and this version stores one set of credentials.
- Restarting the daemon cancels every pending question. `--stop` refuses while any are outstanding unless you pass `--force`.

## What the cards look like

```bash
node scripts/preview.mjs && open card-preview.html
```

MIT
