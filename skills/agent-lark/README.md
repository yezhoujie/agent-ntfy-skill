# herdr-lark

[![skills.sh](https://skills.sh/b/tcyufeng/herdr-lark)](https://skills.sh/tcyufeng/herdr-lark)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

**English** | [简体中文](./README.zh-CN.md)

Reach the agent session **already running** in your [herdr](https://herdr.dev) pane — from Feishu/Lark on your phone.

When it hits something only you can decide, it pushes a card with buttons. You tap one or type a sentence, and the answer lands back in that same session, with all of its context — not a fresh one. Images and voice notes you send go the other way, straight into the pane.

No server and no public URL: the daemon dials out over Feishu's WebSocket. The app is created by scanning a QR code — **no workspace-admin approval needed**, a personal account is enough.

```bash
npx skills add tcyufeng/herdr-lark
```

## Quick start

**1. Put it on your PATH.** The shipped `dist/cli.mjs` is a single self-contained file — no `npm install`, no build step:

```bash
ln -sf "$PWD/.agents/skills/herdr-lark/dist/cli.mjs" ~/.local/bin/herdr-lark
```

**2. Scan a QR code to create the app.**

```bash
herdr-lark setup
```

A QR code is drawn in the terminal — scan it with **Feishu on your phone** (or open the link printed underneath if the terminal renders it badly). The confirmation page lists the permissions being requested; approve, and the app is created on the spot. Credentials go straight into your OS keychain.

**3. Turn it on — the group is created for you.**

```bash
cd <your project> && herdr-lark away on
```

One command gets everything ready: start the daemon → **create a Feishu group for this project** (or reuse the existing one) → flip the switch. Open Feishu and the group is there; every question from this project lands in it.

**One project, one group** — whichever group you speak in is the project you are speaking to, so instructions never reach the wrong agent.

## Make "I'm heading out" work

Drop the rule into your agent's always-loaded rules directory and you never have to remember a command:

```bash
cp .agents/skills/herdr-lark/examples/remote-mode-rule.md ~/.claude/rules/
```

| You say | The agent runs |
|---|---|
| "I'm heading out", "reach me on my phone" | `herdr-lark away on` |
| "I'm back" | `herdr-lark away off` |

While remote mode is on, every reply it writes in the terminal is mirrored **verbatim** into the group, decisions arrive as cards with buttons, and you get a push when the agent is stuck on a prompt only you can answer.

Prefer slash commands? Copy `examples/away.md` and `examples/back.md` into `~/.claude/commands/` for `/away` and `/back`.

## No notification on your phone?

Feishu suppresses mobile push while you are online on desktop. On your phone: Feishu → Settings → Notifications, and turn that off.

## More

- **Full guide**: [English](./docs/guide.md) · [中文](./docs/guide.zh-CN.md) — credential resolution order, environment variables, file locations, known limits
- **What the agent reads**: [SKILL.md](./SKILL.md) — field contract, exit codes, how to write a question worth answering
- **The rule**: [examples/remote-mode-rule.md](./examples/remote-mode-rule.md)

Only exercised end to end on macOS. The Linux and Windows credential stores are written per platform but untested — pull requests welcome.

MIT
