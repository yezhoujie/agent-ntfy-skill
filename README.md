# agent-ntfy-skill

[![skills.sh](https://skills.sh/b/yezhoujie/agent-ntfy-skill)](https://skills.sh/yezhoujie/agent-ntfy-skill)
[![test](https://github.com/yezhoujie/agent-ntfy-skill/actions/workflows/test.yml/badge.svg)](https://github.com/yezhoujie/agent-ntfy-skill/actions/workflows/test.yml)

A skill that lets any AI coding CLI push the decisions it cannot make on its own to your phone via
[ntfy](https://ntfy.sh), and bring your verdict — or any instruction — straight back into the agent's session.
It can also push one-way notifications to the same phone. No server, no fixed IP, no paid service; Python 3
standard library only. Install [herdr](https://herdr.dev) first if you can: phone → agent messages need it,
everything else works without.

让任意 AI coding CLI 把它自己拿不定的事经 ntfy 推到你的手机，再把你的裁决或任何指令直接送回 agent 的会话；也能往同一部手机推单向通知。

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy       # into the current project
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy -g    # for all projects (see the README before using -g on a machine that already has a hand-made copy)
```

The skill lives in [`skills/agent-ntfy/`](skills/agent-ntfy/). macOS is tested end to end on real machines; Linux and Windows
have unit-test coverage on CI only — no end-to-end test in a real environment yet, pull requests welcome.

- Setup, platform support, troubleshooting, security notes, upgrading: [skills/agent-ntfy/README.md](skills/agent-ntfy/README.md) (English) · [skills/agent-ntfy/README.zh-CN.md](skills/agent-ntfy/README.zh-CN.md)（中文）
- What the agent reads: [skills/agent-ntfy/SKILL.md](skills/agent-ntfy/SKILL.md)
- Releases: [CHANGELOG.md](CHANGELOG.md) · License: [MIT](LICENSE)
