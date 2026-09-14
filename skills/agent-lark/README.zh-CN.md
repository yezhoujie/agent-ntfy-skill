# herdr-lark

[![skills.sh](https://skills.sh/b/tcyufeng/herdr-lark)](https://skills.sh/tcyufeng/herdr-lark)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

[English](./README.md) | **简体中文**

把**已经在 [herdr](https://herdr.dev) 窗格里跑着**的 agent 会话接到飞书。

它遇到要你拍板的事，推一张带按钮的卡片到你手机。你点一下或打一句话，答复回到**那个会话**里——那个已经跑了很久、带着全部上下文的会话，而不是新开一个。你发的图片和语音会反向注入回窗格。

不需要服务器，也不需要公网地址：daemon 主动出站连飞书的 WebSocket。应用扫码就能建，**不用企业管理员审核**，个人版账号就够。

```bash
npx skills add tcyufeng/herdr-lark
```

## 三步跑起来

**1. 挂到 PATH 上。** 仓库里带的 `dist/cli.mjs` 是**自包含单文件**——不用 `npm install`，也不用构建：

```bash
ln -sf "$PWD/.agents/skills/herdr-lark/dist/cli.mjs" ~/.local/bin/herdr-lark
```

**2. 扫码建应用。**

```bash
herdr-lark setup
```

终端会画出一个二维码——用**飞书手机端扫**（终端渲染得不好就点它下面打印的那个链接）。确认页上会列出要授权的权限，点同意，应用当场就建好，凭据直接进系统钥匙串。

**3. 开启，群自动建好。**

```bash
cd <你的项目> && herdr-lark away on
```

一条命令把整条通路备齐：起 daemon → **在飞书里给这个项目新建一个群**（已经有就复用）→ 打开开关。打开飞书就能看到那个群，以后这个项目的提问都发在里面。

**一个项目一个群**——你在哪个群说话就是对哪个项目说，指令不会发给错的 agent。

## 让「我走了」直接生效

把规则拷进 agent 的常驻规则目录，之后你只要说话，不用记命令：

```bash
cp .agents/skills/herdr-lark/examples/remote-mode-rule.md ~/.claude/rules/
```

| 你说 | agent 执行 |
|---|---|
| 「我走了」「有事发手机」 | `herdr-lark away on` |
| 「我回来了」 | `herdr-lark away off` |

远程模式开着时，它在终端写的每一句回复都会**逐字**同步到群里，要你拍板的事推成带按钮的卡片，它卡在只有你能回答的提示上时也会推给你。

想用斜杠命令的话，把 `examples/away.md` 和 `examples/back.md` 拷进 `~/.claude/commands/`，就有了 `/away` 和 `/back`。

## 手机通知不响？

飞书在你电脑端在线时会抑制手机推送。手机飞书 → 设置 → 通知，把那个开关关掉。

## 更多

- **完整文档**：[English](./docs/guide.md) · [中文](./docs/guide.zh-CN.md)——凭据解析顺序、环境变量、文件位置、已知边界
- **agent 读的那份**：[SKILL.md](./SKILL.md)——字段契约、退出码、怎么写一个值得回答的问题
- **规则示例**：[examples/remote-mode-rule.md](./examples/remote-mode-rule.md)

只在 macOS 上端到端跑过。Linux 和 Windows 的凭据存储按平台写了但没实测，欢迎 PR。

MIT
