# herdr-lark

[English](./guide.md) | **简体中文**

把 **herdr 窗格里正在跑的** agent 会话接到飞书。

它遇到要你拍板的事，推一张带按钮的飞书卡片到你手机；你点一下或打一句话，答复回到**那个已经跑了两小时、带着全部上下文的会话**里——而不是新开一个。

> 和「从飞书启动一个新 agent」的桥方向相反。那类工具解决的是"我不在电脑前，想让本机 agent 干点新活"；这个解决的是"我在干活，人走开了，会话继续跑，有事找我"。

## 需要什么

- **Node.js ≥ 20.12**
- **[herdr](https://herdr.dev)** —— 答复要注入回终端窗格，靠它
- **一个飞书账号** —— 个人版就行，应用扫码创建，**不需要企业管理员审核**

## 装

仓库里带的 `dist/cli.mjs` 是**自包含单文件**——飞书 SDK 已经打进去了，不用装依赖也不用构建：

```bash
npx skills add tcyufeng/herdr-lark      # 或者 git clone
ln -sf "$PWD/.agents/skills/herdr-lark/dist/cli.mjs" ~/.local/bin/herdr-lark
```

要改代码才需要自己构建：`npm install && npm run build`。

## 配

```bash
herdr-lark setup            # 终端出二维码，用飞书扫；应用当场建好
cd <你的项目> && herdr-lark away on
```

`setup` 的确认页会列出要授权的权限：收发消息、以应用身份发消息、群内免 @ 收消息、建群、上传下载资源、语音转文字，外加 `im.message.receive_v1` 事件和 `card.action.trigger` 回调。少了哪个可以 `herdr-lark setup --update` 补。

`away on` 一条命令把整条通路备齐：检查凭据 → 后台起 daemon → 给这个项目建飞书群（已有则复用）→ 打开开关。

已经有应用了就跳过扫码，**secret 从环境变量读，不走命令行**（argv 全机器可见）：

```bash
HERDR_LARK_APP_ID=cli_xxx HERDR_LARK_APP_SECRET=xxx herdr-lark setup
```

## 用

日常你不用碰这些命令——把 [`examples/remote-mode-rule.md`](../examples/remote-mode-rule.md) 拷到 `~/.claude/rules/`，然后直接对 agent 说「我走了」「我回来了」就行。

```bash
herdr-lark ask <<'JSON'      # 推一张提问卡，阻塞等答复，答复到 stdout
{"title": "...", "doing": "...", "description": "...", "blocker": "...",
 "options": [{"id":"keep","label":"保留固定目录","consequence":"出问题有现场可看，代价是目录越攒越多"},
             {"id":"wipe","label":"连历史一起清掉","consequence":"不可恢复","danger":true}],
 "recommend": "keep", "reasoning": "...", "question": "...", "lang": "zh"}
JSON

herdr-lark say --title "改完了" <<'EOF'   # 把终端回复同步到群（远程模式下每次回复都发）
正文，markdown。表格、列表、代码块都能渲染。
EOF

herdr-lark notify <<'JSON'   # 重大事项通知，不阻塞
{"title": "迁移完成", "body": "..."}
JSON

herdr-lark send-file shot.png --caption "现在的版式"
herdr-lark status
```

字段含义、退出码、写卡片的规矩：[SKILL.md](../SKILL.md)。

### 三种卡片

| | 颜色 | 什么时候 |
|---|---|---|
| 🤔 | 蓝 | `ask` —— 要你拍板，带按钮，调用方阻塞等你 |
| 💬 | 青 | `say` —— 终端回复的逐字同步 |
| 📣 | 浅蓝 | `notify` —— 重大事项，不用你回 |

`ask` 的按钮**点一下就锁死**（改写后的卡片随回调原路返回，没有重复点击的空窗）。标了 `"danger": true` 的选项是红色按钮 + 二次确认弹窗，而且**永远不能是推荐项**——校验会直接拦下。

### 反向：手机 → 终端

群里没有挂着问题时，你发的任何消息都会注入回该项目的 herdr 窗格，前缀 `[herdr-lark remote] `。**发图片也行**——自动下载到本机，路径附在注入的文本里，agent 直接能读。**语音会转成文字**，走飞书的语音识别。

注入失败（agent 正卡在要你确认的提示上、窗格没了）时，群里会收到一张回执卡。

### agent 状态推送

`away on` 后，agent **卡在需要你确认的提示上**时会推一张卡——你不在就永远卡着，这是真的需要人。

「干完了」**默认不推**：每轮对话结束都会触发，你在键盘前时纯属噪音。要的话 `away on --idle 30`，只有跑满 30 分钟的长任务结束才推。

## 一个项目一个群

项目 = git toplevel（不在 git 里就是 cwd），worktree 和 submodule 各算一个。

不是为了好看：单聊里"随口发一句话"没有任何项目归属信息，只能靠"最近活跃"猜，猜错就是把指令注入给了**另一个项目的 agent**，而它会照做。群还能按项目单独设免打扰，正好命中"人走开了"这个场景。

群自动建、自动复用——按**项目绝对路径**匹配（写在群的 description 里），所以丢了本地绑定记录也不会重复建，两个同名的 worktree 也不会认错。

## 凭据

解析顺序，高到低：

1. 环境变量 `HERDR_LARK_APP_ID` / `HERDR_LARK_APP_SECRET`
2. env 文件 `~/.config/herdr-lark/.env`（`HERDR_LARK_ENV_FILE` 可改）
3. **系统钥匙串** —— macOS `security` · Linux `secret-tool` · Windows DPAPI。`setup` 默认写这里
4. `~/.config/herdr-lark/credentials.json`，0600，权限过宽会警告
5. 通用的 `LARK_APP_ID` / `LARK_APP_SECRET`

第 5 层压在最后，是因为好几个飞书工具都读这对名字，同一台机器跑两个会串。

`herdr-lark status` 会把五层逐行列出来，告诉你实际命中了哪层——**但不会打印任何值**。secret 也永远不走 argv。

## 东西放在哪

| 路径 | 内容 |
|---|---|
| `~/.herdr-lark/daemon.sock` | 本地 IPC |
| `~/.herdr-lark/bindings.json` | 项目 ↔ 群 ↔ 窗格 |
| `~/.herdr-lark/daemon.log` | 只记 id 和状态变化，**不记消息内容** |
| `~/.herdr-lark/media/` | 手机发来的图片和文件 |
| `<项目根>/.herdr-lark/state.json` | 远程模式开关、群 id、窗格 id（自带 `.gitignore`） |

## 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `HERDR_LARK_HOME` | `~/.herdr-lark` | 状态目录 |
| `HERDR_LARK_STORE` | 有钥匙串就用钥匙串 | `keychain` / `file` / `none` |
| `HERDR_LARK_KEYCHAIN` | `herdr-lark` | 钥匙串 service 名 |
| `HERDR_LARK_ENV_FILE` | `~/.config/herdr-lark/.env` | env 文件位置 |
| `HERDR_LARK_LANG` | `zh` | 卡片固定文案语言（`zh` / `en`） |

## 已知边界

- **只在 macOS 上实跑过。** Linux（`secret-tool`）和 Windows（DPAPI）的凭据存储按平台写了但没实测。
- 一个项目**同时只能挂一个问题**——第二个 `ask` 直接退 4。文本答复无法关联到具体卡片，所以不做并发。
- 一个飞书自建应用只服务一个租户。企业号和个人号要各建一个应用，当前版本只存一份凭据。
- 重启 daemon 会取消所有待答问题（有问题挂着时 `--stop` 会拒绝，除非 `--force`）。

## 卡片长什么样

```bash
node scripts/preview.mjs && open card-preview.html
```

MIT
