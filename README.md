# agent-ntfy-skill

[![skills.sh](https://skills.sh/b/yezhoujie/agent-ntfy-skill)](https://skills.sh/yezhoujie/agent-ntfy-skill)
[![test](https://github.com/yezhoujie/agent-ntfy-skill/actions/workflows/test.yml/badge.svg)](https://github.com/yezhoujie/agent-ntfy-skill/actions/workflows/test.yml)

Two skills that let any AI coding CLI push the decisions it cannot make on its own to your phone, and bring
your verdict — or any instruction — straight back into the agent's session. Same idea, two channels:
**agent-ntfy** goes through [ntfy](https://ntfy.sh), **agent-lark** through a Feishu / Lark group. Both can
also push one-way notifications; agent-lark can send files too. Install [herdr](https://herdr.dev) first if
you can: phone → agent messages need it, everything else works without.

两个 skill，让任意 AI coding CLI 把它自己拿不定的事推到你的手机，再把你的裁决或任何指令直接送回 agent 的会话。
同一件事、两条通道：**agent-ntfy** 走 [ntfy](https://ntfy.sh)，**agent-lark** 走飞书群。

Not sure which one? [COMPARISON.md](COMPARISON.md) (English) · [COMPARISON.zh-CN.md](COMPARISON.zh-CN.md)（中文）.

## agent-ntfy

Pushes each question as an ntfy notification with one button; you tap or type in the ntfy app. No server, no
fixed IP, no account, no paid service; Python 3 standard library only.

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy       # into the current project
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy -g    # for all projects (see the README before using -g on a machine that already has a hand-made copy)
```

The skill lives in [`skills/agent-ntfy/`](skills/agent-ntfy/). macOS is tested end to end on real machines; Linux and Windows
have unit-test coverage on CI only — no end-to-end test in a real environment yet, pull requests welcome.

- Setup, platform support, troubleshooting, security notes, upgrading: [skills/agent-ntfy/README.md](skills/agent-ntfy/README.md) (English) · [skills/agent-ntfy/README.zh-CN.md](skills/agent-ntfy/README.zh-CN.md)（中文）
- What the agent reads: [skills/agent-ntfy/SKILL.md](skills/agent-ntfy/SKILL.md)
- Making it apply for the whole session (a ready-made rule for the agent, teams included): [README §13](skills/agent-ntfy/README.md#13-integration-keeping-the-skill-in-force-for-the-whole-session) · [`examples/remote-mode-rule.md`](skills/agent-ntfy/examples/remote-mode-rule.md)

## agent-lark

Pushes each question as a Feishu / Lark card with one button per option (or tick boxes) into a group bound to
the project; you tap, tick or type in Feishu, and photos, files and voice notes you send there reach the agent
too. Needs a Feishu custom app, created by scanning a QR code with your own account — no admin approval;
Node.js 22 or newer, one self-contained file.

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-lark       # into the current project
npx skills add yezhoujie/agent-ntfy-skill --skill agent-lark -g    # for all projects
```

The skill lives in [`skills/agent-lark/`](skills/agent-lark/). macOS is tested end to end on real machines, inside herdr; Linux and Windows
have unit-test coverage on CI only — no end-to-end test in a real environment yet, pull requests welcome.

- Setup, platform support, troubleshooting, security notes, upgrading: [skills/agent-lark/README.md](skills/agent-lark/README.md) (English) · [skills/agent-lark/README.zh-CN.md](skills/agent-lark/README.zh-CN.md)（中文）
- What the agent reads: [skills/agent-lark/SKILL.md](skills/agent-lark/SKILL.md)
- Making it apply for the whole session (a ready-made rule for the agent, teams included): [README §14](skills/agent-lark/README.md#14-integration-keeping-the-skill-in-force-for-the-whole-session) · [`examples/remote-mode-rule.md`](skills/agent-lark/examples/remote-mode-rule.md)

## Repository

- Releases: [CHANGELOG.md](CHANGELOG.md) — one section per skill; tags are `agent-ntfy/vX.Y.Z` and `agent-lark/vX.Y.Z`
- License: [MIT](LICENSE)
