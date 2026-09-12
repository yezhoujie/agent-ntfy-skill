# agent-ntfy-skill

A skill that lets any AI coding CLI push the decisions it cannot make on its own to your phone via
[ntfy](https://ntfy.sh), and bring your verdict — or any instruction — straight back into the agent's session.
No server, no fixed IP, no paid service; Python 3 standard library only. Install [herdr](https://herdr.dev) first if you can: phone → agent messages need it, everything else works without.

让任意 AI coding CLI 把它自己拿不定的事经 ntfy 推到你的手机，再把你的裁决或任何指令直接送回 agent 的会话。

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy -g
```

- Setup, troubleshooting, security notes: [skills/agent-ntfy/README.md](skills/agent-ntfy/README.md) (English) · [skills/agent-ntfy/README.zh-CN.md](skills/agent-ntfy/README.zh-CN.md)（中文）
- What the agent reads: [skills/agent-ntfy/SKILL.md](skills/agent-ntfy/SKILL.md)
