# agent-lark

[English](README.md) · 中文

让任意 AI coding CLI 把它自己拿不定的事经[飞书 / Lark](https://www.feishu.cn) 推到你的手机，再把你的裁决——点一下、勾几项、或打一句话——直接送回提问的那个 agent 会话；你主动发的文字、图片、语音也会落进同一个会话。
不需要服务器、不需要公网地址：daemon 主动出站连飞书；应用用手机扫码就能建，不用企业管理员审核。

本文写给装它的人。agent 读的是 [SKILL.md](SKILL.md) 与 `references/`，你不必向它解释这个工具。它是同一仓库里 `agent-ntfy` 的飞书版；两者的比较见[仓库 README](../../README.md)。

## 目录

1. 工作原理
2. 前提（2.1 平台支持 · 2.2 herdr 可选）
3. 安装
4. 配置：三种方式（4.1 手动 · 4.2 让 agent 帮忙 · 4.3 直接说 · 4.4 权限清单 · 4.5 凭据从哪读）
5. 第一条提问，从头到尾
6. 东西放在哪
7. 日常维护
8. 出问题了怎么办
9. 安全须知
10. 已知边界
11. 环境变量
12. CLI 参考
13. 版本与升级
14. 集成方式：让 skill 在整个会话周期里生效

## 1. 工作原理

```
agent ──ask（stdin 里的 JSON）──▶ agent-lark ──本机 socket──▶ daemon ──出站连飞书──▶ 你的手机
      ◀── 回复经 stdout 返回 ───            ◀────────────         ◀── 事件 ────────     ◀── 点按钮 / 勾选 / 打字
                                                                      │
                                                            没有提问在等？
                                                                      ▼
                                                     注入到 agent 所在的 herdr 窗格
```

- agent 交来一段含 8 个必填字段的 JSON；CLI 把它渲染成飞书卡片——每个选项一个按钮，或者一组复选框加一个提交按钮——推进这个项目的群。agent 阻塞等你点按钮、勾选或打字，你的回复原样返回给它。它也可以用 `notify` 推一张单向卡片（标题 + 正文，无按钮）然后接着干活，用 `send-file` 发截图或文件。
- 常驻 **daemon** 独占与飞书的那条连接。没有提问在等的时候你在群里发的任何内容，会带着 `[agent-lark remote] ` 前缀作为一句指令注入 agent 的会话；送达后你那条消息会被贴上 `Get` 表情（只有这一步需要 herdr；不装时哪些能用见 §2.2）。
- **一个项目一个群。** 项目 = agent 工作目录所在的 git 仓根（不在仓里就是那个目录）；`away on` 给它建一个飞书群——或者把它以前用过的群拿回来——这个项目发的一切都进那个群。你在哪个群说话就是对哪个项目说，指令不会发给错的 agent。
- 通路只搬运文字和文件：不解释、不代答。
- 本机 socket 在 macOS / Linux 上是 unix socket，在 Windows 上是命名管道（§6）。

## 2. 前提

- Node.js 22 或更新。CLI 是一个自包含单文件（`dist/cli.mjs`，飞书 SDK 已打进去）：不用 `npm install`，也不用构建
- 一个飞书 / Lark 账号——个人版就够。自建应用用手机扫码创建（§4.1）；别人建好的应用也能用（§4.1 第 2 种）
- 跑 daemon 的机器能出站访问飞书服务器
- 本文与 SKILL.md 里的示例是 POSIX shell 形态（heredoc、`alias`）。Windows 上请在 Git Bash 或 WSL 里跑这些命令；daemon 与 CLI 本身原生可跑
- [herdr](https://herdr.dev)，可选——见 §2.2

### 2.1 平台支持

| 平台 | 状态 |
|---|---|
| macOS | 全链路在真机上实测过：扫码 `setup`、daemon、`away on` 建群 / 改名 / 解绑 / 拿回旧群、`ask`（按钮、复选框、打字回答挂着的提问、加急）、`notify`、`send-file`、手机主动发的文字 / 图片 / 语音在 herdr 内注入、「等你输入」卡、`daemon --stop` 带与不带 `--force`。凭据存钥匙串 |
| Linux | **只有 CI 单元测试**（ubuntu-latest，Node 22 与 24）。没有做端到端真实环境测试——欢迎提 PR。装了 libsecret（`secret-tool`）时凭据存那里，否则存 `0600` 文件 |
| Windows 10 / 11 | **只有 CI 单元测试**（windows-latest，Node 22 与 24，含命名管道传输）。没有做端到端真实环境测试——欢迎提 PR。原生可跑；shell 示例请走 Git Bash / WSL。凭据存 DPAPI 加密文件。经 herdr 的手机 → agent 注入在 Windows 上未验证 |

### 2.2 herdr 可选：不装的话留下什么、失去什么

[herdr](https://herdr.dev) 是本 skill 用来把文字送*进* agent 会话的终端复用器（macOS / Linux 上 `brew install herdr`；其他平台见 https://herdr.dev）。它是唯一可选的一环，依赖它的只有下面这些：

| 不装 herdr 也能用 | 必须有 herdr |
|---|---|
| `ask` 整条链路：手机上出卡片 → 点按钮 / 勾选 / 打字 → 回复回到 stdout → 退出码；`notify`；`send-file` | 手机 → agent 的消息：没有提问在等时你主动发的指令、图片、语音，或者回一张已经回答 / 超时 / 取消的旧卡片 |
| daemon 与其余全部子命令：`setup`、`away`、`rename`、`unbind`、`bind`、`status` | 🔔「等你输入」卡：远程模式开着、agent 卡在只有人能回答的提示上（权限确认、选择题）时推送 |
| 群与绑定两种情况下都按项目算；不装 herdr 时不会记录窗格，也就没有可注入的地方 | |

这种消息不会静默丢掉。daemon 会在群里回一张橙色回执卡，标题「[<项目目录名>] 没能送达」，正文写明原因（这个项目没记录过 herdr 窗格；记录的窗格已经不在了；这台机器没有 herdr 或 herdr 没在跑；agent 正卡在提示上收不了输入）；在 herdr 外跑 `away on` 末尾也会多一句 `Not inside herdr: messages sent from the phone are not injected anywhere, and there is no stuck-on-a-prompt alert.`

为什么非 herdr 不可：注入就是往目标 agent 的终端（PTY）里写一行文本，`herdr agent prompt` 是对任何 agent CLI 都通用的唯一办法；本 skill 没有别的兜底机制。

## 3. 安装

装进当前项目（缺省 skill 落在 `./.agents/skills/agent-lark`，并从 `./.claude/skills/agent-lark` 打一个符号链接过去；用 `-a <agent>` 只指定一个非 universal 的 agent 时 CLI 会改为拷进那个 agent 自己的目录）：

```bash
npx skills add yezhoujie/agent-remote-communication-skills --skill agent-lark
```

要给所有项目用，加 `-g`：文件放到 `~/.agents/skills/agent-lark`，`~/.claude/skills/agent-lark` 变成指向它的符号链接。

> **`-g` 的警告。** 如果 `~/.claude/skills/agent-lark` 已经是一个真实目录（你手动拷进去的副本），`skills` CLI 会把它删掉、换成符号链接。先备份。（这是读 CLI 源码得出的，没有在真实目录上试过。）

任何能把 `skills/agent-lark/` 放到 agent 加载 skill 位置的办法都行（`git clone` 后拷目录也一样）。要钉住某个版本，安装时带 git ref（§13）。

CLI 就是目录里的 `dist/cli.mjs`。它自己的提示文案里管自己叫 `agent-lark`；配一个 alias 下面的命令会短很多（文件带可执行位，往 `PATH` 里的目录打一个符号链接也行）：

```bash
alias agent-lark='node "<skills/agent-lark 的路径>/dist/cli.mjs"'
```

## 4. 配置：三种方式

每台机器只做一次的事：建一个飞书应用（或复用已有的），把凭据存起来。之后的一切——起 daemon、给项目建群、打开开关——都是一条命令 `away on`（§5）。走到这一步有三种方式，终点相同。

### 4.1 手动：`agent-lark setup` 一步步引导

```bash
agent-lark setup
```

在终端里跑，开头是一个菜单（`setup` 的每一行都中英并排打印，下面只列中文那一半）：

```
怎么接入飞书？
  1) 扫码新建一个应用（用飞书扫终端里的二维码）
  2) 复用一个已有的应用（输入 App ID 与 App Secret）
选 [1/2]：
```

输 `1`、`2` 以外的东西会再问一次。stdin 不是终端（管道、脚本）时没有菜单，直接走扫码。

**1——扫码新建应用。** `setup` 向飞书申请扫码注册，在终端里画出二维码（ANSI 字符画），并**紧跟着把同一个链接以一行文本打出来**，终端渲染得不好也能点开。用手机飞书扫码；确认页会列出要授权的权限（§4.4）；同意后应用当场建好。二维码几分钟内有效（会打出过期时间；之后每分钟一行 `还在等你扫……`）；过期就再跑一次 `setup`（退 4）。等待期间网络抖一下会作废这张码：`setup` 自动重新申请，最多三次。成功后：

```
✅ 应用已绑定，凭据保存到：macOS Keychain (service: agent-lark)（明文不会出现在任何输出里）。
下一步：
  agent-lark daemon --detach
  cd <project> && agent-lark away on --name "<task>"
```

**2——复用一个已有的应用**（`agent-lark setup --reuse` 不出菜单、直接进这一支）。两个提示，然后连一次飞书：

```
App ID（cli_ 开头）：cli_xxxxxxxx
App Secret（输入不回显）：
正在连一次飞书确认这对凭据可用……
✅ 凭据可用，应用名「我的 agent 应用」
凭据已保存到：macOS Keychain (service: agent-lark)
这个应用要在开发者后台手动开通（应用 → 权限管理 → 开通权限）：
  im:message
  im:message:send_as_bot
  im:message.group_msg
  im:chat
  im:resource
  im:message.urgent
  speech_to_text:speech
事件订阅：im.message.receive_v1（订阅方式选「使用长连接接收事件」）· 回调：card.action.trigger（同样选长连接）
开通后发布一个版本，权限才生效。
下一步：
  agent-lark daemon --detach
  cd <project> && agent-lark away on --name "<task>"
```

- App ID 必须是 `cli_` 加字母数字（开发者后台「凭证与基础信息」里看）；不是就再问一次。Secret 盲打——什么都不回显——之后也不会出现在任何输出里，连错误信息里都会打成 `***`。
- 飞书不认的一对会打出飞书的错误码和信息，再问一次；连续三次不通过退 1，什么都不存。`setup` 顺带记下应用所有者（新建群时拉进去的那个人）。
- 复用的应用不是这个工具建的，它的权限、事件订阅和卡片回调要**手动**在开发者后台开通，两者的订阅方式都选长连接，然后发布一个版本（§4.4）。`setup` 把清单打出来，照着勾。

**不论哪种方式：** 凭据存哪取决于平台——有钥匙串就存钥匙串，否则存 `0600` 文件——也可以用 `AGENT_LARK_STORE` 强制指定（§11）。之后再跑 `setup`，它只报 `已经有凭据了（来自 …）` 退 0，`--reuse` 也一样。两个旗标能改变这一点：`--update` 对**同一个**应用重新扫码授权（终端上先出菜单，选 1）——扫码建的应用补权限就靠它；`--reset` 先删掉存着的凭据，所以换应用是 `agent-lark setup --reset --reuse`（或只带 `--reset`，走扫码）。

### 4.2 让 agent 帮忙：`/agent-lark setup`、`on`、`off`

agent 认三个参数（SKILL.md「Invoked with an argument」）：`/agent-lark setup` 跑引导式设置、只报结果；`/agent-lark on` 给当前项目打开远程模式（§5），没有凭据就先走设置；`/agent-lark off` 关闭。

`setup` 时 agent 先问你走哪条路——扫码新建、还是复用已有——不会替你选。

- **扫码**：agent 替你跑 `agent-lark setup`。它一般没法把屏幕给你看，就把二维码下面那行链接交给你（或者把链接自己渲成二维码图片打开）；你扫完，它报结果。
- **复用**：App ID、尤其是 App Secret 不能经 agent 的手。在 [herdr](https://herdr.dev) 里，agent 跑 `agent-lark setup --reuse` 会在它自己的窗格下方开一个新终端窗格、在那里跑交互式设置；**你在那个窗格里输入 App ID 与 Secret**。结束时会有一行注入回 agent 的会话，它才能接着干——`[agent-lark] setup: credentials stored for cli_xxxxxxxx (我的 agent 应用); the scopes must be enabled in the developer console before use`（或 `… failed: <原因>`、`… interrupted before any credentials were stored`、`… credentials already stored (…); nothing changed. …`）——成功时那个窗格再问一句 `关掉这个窗格？[Y/n]`（失败则留着不关，原因还看得到）。herdr 外开不了窗格：`setup --reuse` 退 4，agent 把要在你自己终端里跑的那条命令原样给你（`node …/dist/cli.mjs --home … setup --reuse`）；跑完告诉 agent 一声。

### 4.3 直接跟 agent 说

「开启远程交互模式」「我走了，有事发手机」——§14 的规则生效着，这一句就够：agent 跑 `away on --name "<任务名>"`，还没有凭据就先走 4.2。这是日常的路；4.1 和 4.2 是第一次在一台机器上用、或换应用时才要的。

### 4.4 权限清单

扫码新建时确认页会请求这些权限（可用 `setup --scopes a,b,c` 覆盖）；复用的应用要在开发者后台手动开通同样这些：

```
im:message
im:message:send_as_bot
im:message.group_msg
im:chat
im:resource
im:message.urgent
speech_to_text:speech
```

外加事件 `im.message.receive_v1` 和卡片回调 `card.action.trigger`，两者都走飞书的**长连接**投递（不需要公网 URL）。`im:message.urgent` 是 `ask --urgent` 要的；`speech_to_text:speech` 是转写语音要的（另外还要付费版租户，§8）。后来发现缺一项？扫码建的应用跑 `agent-lark setup --update` 重新扫码补到同一个应用上；复用的应用去开发者后台改（改完发布一个版本）。

### 4.5 凭据从哪读

解析顺序，高的优先——共用的机器上你得知道哪一层赢：

1. 环境变量 `AGENT_LARK_APP_ID` / `AGENT_LARK_APP_SECRET`（外加 `AGENT_LARK_OWNER_OPEN_ID`，见 §11）——运行时覆盖，不写进任何地方
2. **系统钥匙串**——macOS `security`、Linux `secret-tool`，Windows 上是 DPAPI 加密文件。`setup` 默认写这里
3. `~/.config/agent-lark/credentials.json`，权限 `0600`（`AGENT_LARK_STORE=file`、或没有钥匙串的平台写这里）；权限过宽会警告

别的都不读——没有 env 文件，也没有别的变量名。`~/.config/agent-lark` 在设了 `XDG_CONFIG_HOME` 时是 `$XDG_CONFIG_HOME/agent-lark`，Windows 上是 `~/AppData/Roaming/agent-lark`。`agent-lark status` 把三层都列出来并标出命中的那层——**永远不打印值**。

## 5. 第一条提问，从头到尾

**第 1 步——给项目打开远程模式。** 在 agent 工作的那个目录里（用 herdr 的话在 agent 所在的窗格里跑，这样窗格会被记下来）：

```bash
cd <project> && agent-lark away on --name "payment refactor"
```

它是一站式的：没有凭据 ⇒ 退 4 并提示 `No Feishu app credentials yet. Run once: agent-lark setup`；没有 daemon 应答就起一个（`daemon: started in the background, pid 12345 (log ~/.agent-lark/daemon.log)`，已经在跑则是 `daemon is already running`）；等 daemon 连上飞书，最多 15 秒（没连上退 3 并带上最后一次连接错误）；然后给项目建群，群名 `payment refactor [<项目目录名>]`，把你拉进去：

```
Created Feishu group "payment refactor [myproject]"
Remote mode is on: decisions, and moments when the agent is stuck on a prompt that needs you, are pushed to this project's Feishu group.
```

打开飞书，群就在那里。不带 `--name` 群名就是 `[<项目目录名>]`。任务名最多 60 个字符（按 code point 数；超了退 1）。

**这个项目以前用过群的时候**（上一个任务结束时跑过 `unbind`，或者本地记录丢了——daemon 还会翻一遍飞书里描述标着这个项目的群），`away on` 不会再建一个。它退 4，把候选逐行列在 stderr——群名、什么时候放掉的、群 id——再说明怎么重跑：

```
agent-lark: this project has no live group, but 1 earlier group(s) could be taken back (renamed) instead of creating another:
old task [myproject]  released 2026-09-02T03:04:05.000Z  oc_xxxxxxxx
ask the user which to reuse (rename) or create new; rerun with --reuse <chatId> or --new
```

选哪个是你的事，不是 agent 的：`away on --reuse oc_xxxxxxxx --name "payment refactor"` 把那个群拿回来并改名（`Took back Feishu group "…"`）；`away on --new --name "…"` 新建一个。SKILL.md 要求 agent 问你，而不是自己挑。

**第 2 步——先问自己一个问题**，看一遍来回：

```bash
agent-lark ask <<'JSON'
{
  "title":       "测试：吃什么甜点",
  "doing":       "验证 agent-lark 能到达这台手机",
  "description": "这是这台机器经 agent-lark 发出的第一条提问，答什么都没有影响。",
  "blocker":     "没有卡点，这是测试。",
  "options": [
    {"id": "cake", "label": "蛋糕", "consequence": "测试通过，而且你想了一下蛋糕"},
    {"id": "pie",  "label": "派",   "consequence": "测试通过，而且你想了一下派"}
  ],
  "recommend": "cake",
  "reasoning": "蛋糕，因为它排在前面。最强的反对意见是派也不错。",
  "question":  "蛋糕还是派？",
  "lang":      "zh"
}
JSON
```

stderr 打一行 `note: sent to the Feishu group, waiting for the answer (up to 43200 s)`，命令阻塞。手机上出现一张蓝色卡片，标题「🤔 [myproject] 测试：吃什么甜点」：分段【在做】【背景】【卡点】、编号的【选项】列表（「1. 蛋糕：… ← 我推荐」）、【我的判断】【你的判断】，然后每个选项一个按钮和一行提示。点 **蛋糕**，终端打印 `蛋糕`；改在群里打「当然是派」，终端就打印 `当然是派`——提问挂着的时候，你在那个群里发的第一条消息**就是**答复。卡片变绿、「✅ … · 已回答」，你的回复在上面、原提问保留在下面；没人回答的卡片变灰（超时后是「⌛ … · 已超时」，默认 12 小时；agent 放弃或 daemon 停掉时是「⚠️ … · 已取消」）。

agent 可能用到的三个变体：`"select": "multi"` 且 `"recommend"` 为数组时渲染成复选框加一个**提交**按钮，回复是勾中的 label 用「、」拼起来；标了 `"danger": true` 的选项是红色按钮加二次确认弹窗，而且永远不能是推荐项；`ask --urgent` 在飞书里给你加急（等待期间卡头是红色），需要 `im:message.urgent` 权限——加急没发出去时提问照常发出，stderr 多一行 `note:` 说明。

**第 3 步——交给 agent。** 它自己读 SKILL.md。没有提问在等时你在群里发的任何内容都注入 agent 的会话（herdr），送达后贴 `Get` 表情；图片或文件以 `[saved: <绝对路径>]` 行的形式到达，agent 直接能打开；语音在租户允许的情况下转成文字（§8）。对某张卡片用飞书的「回复」时，注入文本前面会加一行 `(reply to: "<卡片标题>")`，「对，就这么办」就不会失去所指。

**通知。** agent 也可以发一张不需要回答的单向卡片：

```bash
agent-lark notify <<'JSON'
{"title": "构建完成", "body": "**测试**：483 条通过。\n\n没有要拍板的事，只是告诉你一声。", "lang": "zh"}
JSON
```

它打印 `Notification sent (a reply from the phone is injected into this pane as an instruction)` 并立即返回：一张浅蓝色的 `📣` 卡片，无按钮、无状态——永远不变色，回复它就是一条普通指令。提问挂着的时候也能发。

**文件。** `agent-lark send-file <路径> [--caption <文字>]` 把一张图片（png / jpg / gif / webp / bmp，≤ 10 MB）或任意文件（≤ 30 MB）发进群并打印 `Sent to the project group`；说明文字先以单独一条消息发出。只有项目目录、`~/.agent-lark/media` 或系统临时目录下的文件能发（§9）。

**关掉，以及收尾。** `agent-lark away off` 只关开关（`Remote mode is off.`）；群和绑定都留着。任务结束时 `agent-lark unbind` 把群放掉：它留在飞书里，下次同目录 `away on` 会把它提出来（第 1 步）。两种情况下 daemon 都继续跑。

## 6. 东西放在哪

| 路径 | 内容 |
|---|---|
| `~/.agent-lark/` | daemon 的状态目录（`AGENT_LARK_HOME` 或 `--home` 可改；权限 `0700`） |
| `~/.agent-lark/daemon.sock` | 本机 socket（macOS / Linux）。Windows 上没有文件：是一条由状态目录路径派生的命名管道 `\\.\pipe\agent-lark-<12 位十六进制>` |
| `~/.agent-lark/daemon.pid`、`daemon.log` | 运行中 daemon 的 pid；只记 id 和状态变化的日志——**从不记消息内容**（但会出现项目路径与群 id） |
| `~/.agent-lark/bindings.json` | 项目 ↔ 群：`root`、`label`、`chatId`、`name`、`paneId`（手机消息注入到哪个窗格）、`away`、`lang`（这个项目最近一张卡的语言，daemon 自己发的卡照它）、`boundAt`、`releasedAt`（群是项目当前活跃群时为 `null`；之后是 `unbind` 的时间，留着以便下次提出来） |
| `~/.agent-lark/media/<hash>/` | 手机发来的图片、文件、语音，每个群一个子目录。daemon 启动时和之后每 24 小时清一次：早于 `AGENT_LARK_MEDIA_TTL_DAYS`（默认 7；`0` 关闭清理）的文件删掉；`daemon --status` 显示还留着多少 |
| 钥匙串 / `~/.config/agent-lark/` | 应用凭据（§4.5） |
| `<项目根>/.agent-lark/state.json` | 项目级开关，旁边有一个自我忽略的 `.gitignore`（内容 `*`，你项目自己的 `.gitignore` 不会被动）。由第一次 `away on` 或 `bind` 创建；没用过 skill 的项目不会被建目录 |

`state.json` 只有四个字段——注入目标留在 `bindings.json` 里：

```json
{"away": true, "chatId": "oc_xxxxxxxx", "target": "/path/to/project", "updated": "2026-09-15T03:01:52.949Z"}
```

`away status` 读它（`remote mode: on  group: oc_xxxxxxxx`；`--json` 打印文件原文，文件不存在时打印 `away: false` 的四个字段）；`away`、`bind`、`unbind` 写它（`unbind` 之后 `chatId` 变成 `null`）。

**第一次 `away on` 之前要知道的一件事：** 群的描述会被设成 `agent-lark · <项目绝对路径>`。这是本地记录丢了之后项目找回自己的群的依据，也意味着你项目目录的绝对路径会存在飞书服务器上，群里每个成员都看得到。建群时群里只有你。

## 7. 日常维护

```bash
agent-lark rename "second task"          # 把活跃群改名为 "second task [<项目目录名>]"
agent-lark unbind                        # 任务结束：放掉这个群（留在飞书里，下次提出来）
agent-lark bind --chat oc_xxxxxxxx       # 直接把项目指到某个群（你自己建的，或重装之后）
agent-lark status                        # 凭据、herdr、daemon、每个项目的群
agent-lark daemon --status               # daemon: pid 12345  connected true  connection connected  pending questions 0  bound projects 1  started …
                                         # media: ttl 7 days, 0.3 MB in 4 files (as of last sweep …)
agent-lark daemon --stop                 # 有提问挂着时拒绝（退 4）；--stop --force 取消提问并停掉
agent-lark send-file shot.png --caption "现在的版式"
```

- `rename` 只对活跃群有效（没有活跃群退 4），并在飞书里改名（飞书拒绝时退 3——机器人只能改自己拥有的群、或群设置允许所有成员改群信息的群，而且必须是群成员）。
- `unbind` 有提问挂着时拒绝（退 4），没绑定时退 1。
- `bind` 收和 `away on` 一样的 `--name` / `--reuse` / `--new`，但不碰开关；`--chat <id>` 直接绑那个群、放掉当前这个（那个群是别的项目的活跃群退 1；有提问挂着退 4）。群描述会被改写成标记本项目。
- `status` 列出活跃绑定（`*` 标当前项目：路径、群名、群 id、`away=`、`pane=`），下面是能拿回来的已放掉的群。
- 换任务、换群、上下文重置都不用停 daemon；它同时管着所有项目的群。升级（§13）或要腾出机器时才停。
- **不要这个飞书应用了**：`setup` 建的是你租户里一个真实的自建应用。要删，先到飞书管理后台（工作台管理 → 应用管理）**停用**它，再到开发者后台删除；只停用的应用凭据还留在本机，换应用时跑 `agent-lark setup --reset`（或 `--reset --reuse`）。

## 8. 出问题了怎么办

**退出码**处处相同，就这五个：0 成功 · 1 输入不合格，什么都没发 · 2 超时，没人回答（只有 `ask`）· 3 通道故障（daemon 没跑、没连上飞书、飞书拒绝了调用；stderr 写明是哪种）· 4 需要人介入。各命令具体返回什么：

| 命令 | 1 | 3 | 4 |
|---|---|---|---|
| `ask` | JSON 或字段不合格（每个问题都会列出）、`--timeout` 不合法 | daemon 没跑、没连上、发送失败 | 项目没绑定；已有提问挂着（一次一个） |
| `notify`、`send-file` | JSON 不合格；文件不存在、不在允许的目录下、不是普通文件、太大 | daemon 没跑、没连上、发送失败 | 项目没绑定 |
| `away on` | 任务名不合法、`--reuse` 与 `--new` 同给、`--reuse` 的群不在候选里 | daemon 起不来、15 秒内没连上飞书、建群失败 | 没有凭据；有旧群可拿回（stderr 列出）；不知道应用所有者是谁；飞书因缺权限拒绝建群 |
| `away off`、`away status` | 子命令不认识 | `away off`：daemon 没跑 | |
| `rename` | 没给名字、名字超 60 个 code point | 没连上；飞书拒绝（附权限提示） | 没有活跃群 |
| `unbind` | 没绑定 | daemon 没跑 | 有提问挂着 |
| `bind` | 同 `away on`；`--chat` 指向别的项目的活跃群 | 同 `away on` | 同 `away on`；有提问挂着时 `--chat` |
| `daemon --status` | 没在跑 | | |
| `daemon --stop` | | 发了停止请求 10 秒后还在应答 | 有提问挂着（用 `--force`） |
| `daemon --detach`、`daemon` | | 已经在跑；10 秒内没应答 | 没有凭据；`bindings.json` 读不了 |
| `setup` | App ID / Secret 连续三次不通过 | 注册失败；`--reuse` 在 herdr 里但开不出窗格；设了 `AGENT_LARK_OFFLINE=1` | 二维码过期；herdr 外没有终端时的 `--reuse`（stderr 给出你自己跑的命令） |
| 任何命令 | 它不认识的选项（`unknown option --xyz`）——早先版本的选项也算，`--name=x` 这种写法也算（值要用空格分开） | | |

每次失败都在 stderr 打一行、前缀 `agent-lark: `；意外崩溃退 3 并打出栈。没有 daemon 在跑时 `daemon --stop` 退 0（`daemon: was not running`，并顺手删掉残留的 pid 文件）；`away off` 同样退 0，本地把项目的 `state.json` 关掉并说一声（`daemon is not running; local state cleared`）。`ask` 超时打 `agent-lark: no answer after 43200 s` 退 2；手机上那张卡变灰。

- **`away on` 退 3、报 `daemon is up but not connected to Feishu: …`。** daemon 起来了，但 15 秒内没和飞书握手成功——凭据不对、没网、飞书挂了。`agent-lark daemon --status` 会显示最后一次错误；daemon 会带退避不断重连（5 秒起、翻倍、最长一分钟），原因排除后再跑一次 `away on` 就行。对一个掉了连接的 daemon 跑 `ask` / `notify` / `send-file` 退 3，报 `not connected to Feishu (…); the daemon keeps retrying, try again shortly`。
- **语音存下来了但没转成文字。** 转写需要 `speech_to_text:speech` 权限**且飞书付费版租户**：飞书官方的语音识别 API 文档注明免费版不支持调用，免费 / 个人版租户即使已开通权限，调用也会返回 HTTP 400 `{"code":99991400,"msg":"request trigger frequency limit"}`。语音文件照常保存、路径照常注入，另附一行说明没能转写、并带上飞书返回的错误码；改打字即可。飞书官方「识别语音文件」文档写明「接口适合 60 秒以内音频识别」，语音请控制在一分钟内。
- **卡片上显示「已读 0/0」。** 那是飞书对机器人所发消息的已读计数，不是送达状态。真正算数的两个标记：`ask` 卡被回答会变绿（`notify` 卡永远不变色）；你主动发的消息送进终端后会被贴上 `Get` 表情——没有表情就是没注入成功，群里会有一张回执卡说明原因（§2.2）。
- **你发的消息收到一张回执卡、没到 agent。** daemon 没有可注入的窗格（跑命令的会话不在 herdr 里，或记录的窗格已经没了）、那台机器上 herdr 没在跑，或者 agent 正卡在只有你能回答的提示上。在 agent 的 herdr 窗格里随便跑一条 `agent-lark` 命令重新记录窗格；最后一种情况回到电脑前处理那个提示。
- **`away on` 列出的旧群你不想要。** 用 `--new` 回答。这份清单只在项目没有活跃群时出现；一个群只要还在 `bindings.json` 里有记录、或还在飞书里且描述带着本项目的标记，就会一直列在里面；没有命令能让它忘掉某一个。

## 9. 安全须知

- **凭据存在系统钥匙串里**（macOS `security`、Linux `secret-tool`），Windows 上是 DPAPI 加密文件；`AGENT_LARK_STORE=file` 则存成 `0600` 的 JSON 文件。**App secret 永远不走 argv**（`setup --reuse` 在终端里读、不回显），除上述存储外不写进 skill 自己创建的任何文件，也不出现在任何输出里——碰巧含有它的错误文本会打成 `***`，`status` 只标出命中了哪层、不打印值。
- **群里的任何人都能操纵你的 agent。** 点一下就是答复；打一句话就是注入 agent 会话的指令（有 herdr 时）。建群时群里只有你；保持这样。通路不过滤内容。
- **内容经过飞书服务器**：提问描述你的项目，图片与文件从飞书下载，群描述带着项目的绝对路径（§6）。别往提问里放密钥。
- **`send-file` 有围栏**：只有真实路径（解析符号链接后）在项目根、`~/.agent-lark/media` 或系统临时目录下的文件才会发出；其余一律拒绝并列出这三个目录。这防止 agent 把机器上的任意文件寄出去。
- **daemon 日志只记 id 和事件**——`ask.sent`、`inject`、`bind` … 带项目路径、群 id、请求 id——从不记提问、回复或消息的文字。
- 要换一个应用从头来：`agent-lark setup --reset`（删掉存着的凭据，然后扫码注册）。旧应用要作废的话去飞书开发者后台删掉它。

## 10. 已知边界

- Linux 与 Windows 只有单元测试覆盖，没有在真实环境里跑过端到端（§2.1）。欢迎带着真机报告提 PR。
- **一个项目同时只挂一个提问。** 第一个还在等时第二个 `ask` 退 4；文本答复无法关联到具体卡片，所以不做并发。不同项目互不阻塞。
- 一个飞书自建应用只服务一个租户，一台机器只存一套凭据：企业号和个人号不能同时接上。
- 停 daemon 会取消所有挂着的提问（卡片变灰，正在等的 `ask` 退 3）。有提问挂着时 `--stop` 拒绝，除非加 `--force`。
- 卡片上限：title ≤ 200 字符且单行；`doing` / `description` / `blocker` / `reasoning` / `question` 各 ≤ 4000 字符；2–5 个选项，label ≤ 60、consequence ≤ 500 字符；`notify` 正文 ≤ 8000 字符。超限会点名字段拒绝，不截断。
- 任务名（`--name`、`rename`）最多 60 个 code point。
- 手机 → agent 的注入需要 herdr（§2.2）。daemon 不判断 agent 忙不忙（你的 CLI 自己排队）；herdr 报告 agent 卡在提示上时改发回执。
- 语音：每条 60 秒以内（飞书「识别语音文件」接口文档给的上限），转写只在付费版租户上可用（§8）。
- 🔔「等你输入」卡每个项目每分钟最多推一次，且只在 `away` 开着时推。
- `setup --reuse` 里 App Secret 的盲打（终端 raw 模式）只在 macOS 上验证过；Windows 真实控制台上没试过。

## 11. 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `AGENT_LARK_HOME` | `~/.agent-lark` | daemon 的状态目录（§6）。命令行的 `--home <目录>` 覆盖它，并会传给用 `--detach` 起的 daemon。用 unix socket 传输时路径别太深：socket 路径超过系统上限会让每条命令都报 `connect EINVAL …/daemon.sock` |
| `AGENT_LARK_APP_ID`、`AGENT_LARK_APP_SECRET` | | 应用凭据，解析顺序第一层（§4.5）。环境变量里的这对压过钥匙串 |
| `AGENT_LARK_OWNER_OPEN_ID` | | 应用所有者的 `open_id`；凭据来自环境变量时，建群要靠它（`setup` 写进钥匙串的条目自带）。没有它，在没有群的项目里 `away on` 退 4（`nobody to invite into a new group`）——改用 `--chat` 绑一个已有的群 |
| `AGENT_LARK_STORE` | 有钥匙串可用时 `keychain`，否则 `file` | `setup` 写哪：`keychain`、`file`（`~/.config/agent-lark/credentials.json`，`0600`）或 `none`（只留在本次运行的内存里） |
| `AGENT_LARK_KEYCHAIN` | `agent-lark` | 钥匙串 service 名（macOS 与 Linux；账户名固定为 `app`） |
| `AGENT_LARK_MEDIA_TTL_DAYS` | `7` | 手机发来的图片、文件、语音在 `~/.agent-lark/media` 下保留几天；`0` 关闭清理。不是整数的值会被拒绝并打警告，改用默认值 |
| `AGENT_LARK_OFFLINE` | | 设为 `1` 时 `setup` 拒绝它的两次联网（扫码注册、凭据校验），退 3——给测试套件和离线机器的护栏；测试 runner 会设它。别的命令不读它 |
| `XDG_CONFIG_HOME` | | 和任何遵守 XDG 的工具一样，改变 `~/.config/agent-lark`（凭据文件）的位置 |
| `HERDR_ENV`、`HERDR_PANE_ID` | herdr 设置 | 自动检测，不用你配：在 herdr 里，`away on` / `away off`、`bind`、`rename`、`ask`、`notify`、`send-file` 会把当前窗格记到项目的绑定上，手机消息就注入到它（`unbind`、`status`、`away status`、`daemon` 不碰它） |

没有语言变量：卡片的固定文案跟着产生它的那条提问或通知的 `lang` 字段走（缺省 `en`）；daemon 自己发的卡（回执卡、🔔 卡）跟着这个项目最近一次的 `lang` 走，之前没有就是英文；agent 读的一切——stdout、stderr、`help`——都是英文；`setup` 两种都打。

## 12. CLI 参考

`agent-lark help`（也可以是 `--help`、`-h` 或不带参数）的输出：

```
agent-lark — reach the agent session running in your terminal from Feishu/Lark

  setup [--update] [--reset] [--scopes a,b]
                                     On a terminal: a menu, create the app by QR code or reuse one; piped: QR code straight away.
                                     Credentials go to the keychain (--update re-authorizes, --reset forgets them first)
  setup --reuse                      Reuse an app you already have: asks for the App ID and the App Secret (not echoed) on the terminal
  setup --reuse --report-to <pane> [--close-pane]
                                     What the agent runs for you in a herdr pane: the result comes back to <pane> as one "[agent-lark] setup:" line
  daemon [--detach|--status|--stop]  Resident process holding the Feishu connection (--stop is refused while a question is pending, unless --force)
  away on [--name <task>] [--reuse <chat_id> | --new]
                                     Remote mode on: daemon up, this project bound to a Feishu group named "<task> [<dir>]"
                                     (exit 4 lists earlier groups to take back; rerun with --reuse or --new)
  away off | status [--json]         Remote mode off / the project's state ({away, chatId, target, updated})
  rename "<task>"                    Rename the project's live group to "<task> [<dir>]"
  unbind                             Let the live group go (it stays in Feishu; the next away on offers it back)
  bind [--chat <id>] [--name <task>] [--reuse <chat_id> | --new]
                                     Bind without switching remote mode on; --chat names a group outright
  ask [--timeout <seconds>] [--urgent]
                                     Read JSON from stdin, push a question card, block until answered (--urgent flags the owner in-app)
  notify                             Read JSON from stdin, push a titled notification card (important things only)
  send-file <path> [--caption <t>]   Send an image or file to the project group
  status                             Daemon and binding overview

Global: --home <dir>  state directory (same as AGENT_LARK_HOME; default ~/.agent-lark)

Exit codes: 0 ok · 1 bad input · 2 timed out, nobody answered · 3 channel failure · 4 a human must act
```

`--home <目录>`（或 `--home=<目录>`）放在命令行任何位置都行；其余选项的值都用空格分开（`--name x`，不是 `--name=x`），命令不认识的选项退 1（`unknown option --xyz`），什么都不做。逐条说明：

- **`setup [--update] [--reset] [--scopes a,b]`**——在终端里是 §4.1 的菜单（扫码或复用）；管道里直接扫码。`--update` 对已存的那个应用重新扫码（补权限或重新授权；终端上先出菜单，选 1）；`--reset` 先删掉存着的凭据；`--scopes` 替换默认权限清单（§4.4）。每一行中英并排打印。
- **`setup --reuse [--report-to <pane>] [--close-pane]`**——不出菜单、直接走复用：在终端里问 App ID 与 App Secret（§4.1 第 2 种）。没有终端时它不问：herdr 里自己开一个窗格、在里面带 `--report-to <调用方窗格> --close-pane` 跑自己，结果以一行 `[agent-lark] setup:` 回来；herdr 外退 4 并打出让你手动跑的命令（§4.2）。
- **`daemon`**——前台跑 daemon（`agent-lark daemon: pid 12345, listening at ~/.agent-lark/daemon.sock, connecting to Feishu in the background`）；Ctrl-C 停。**`--detach`** 在后台起并最多等 10 秒它应答；**`--status`** 打印 §7 里那两行（连接失败时多一行 `last error: …`）；**`--stop [--force]`** 请它停下并最多等 10 秒。绝不要把 daemon 当作 agent 自己 shell 的后台任务起：agent 一退出它就没了。
- **`away on [--name "<任务名>"] [--reuse <chat_id> | --new]`**——§5 第 1 步。**`away off`** 关开关（daemon 没跑也照样写项目的 `state.json`，退 0）。**`away status [--json]`** 打印项目的 `state.json`（§6），不和 daemon 说话。
- **`rename "<任务名>"`**、**`unbind`**、**`bind [--chat <id>] [--name "<任务名>"] [--reuse <chat_id> | --new]`**——§7。
- **`ask [--timeout <秒>] [--urgent]`**——从 stdin 读一个 JSON 对象（用 heredoc；stdin 是终端时拒绝），发出去之前先校验，推卡片并阻塞。回复打到 stdout：点中的 label、勾中的 label 用「、」拼接、或打字的原文。默认超时 43 200 秒（12 小时）。字段契约见 [SKILL.md](SKILL.md) 与 [references/message-spec.md](references/message-spec.md)。
- **`notify`**——从 stdin 读 `{"title", "body", "lang"}`；正文是 Markdown。打印 `Notification sent (…)`。
- **`send-file <路径> [--caption <文字>]`**——§5「文件」。
- **`status`**——凭据（三层里命中了哪层）、`herdr: inside herdr, pane wG:p3` / `not inside herdr`、daemon 那一行，然后是每个项目的活跃群和已放掉的群。

各种失败的 stderr 原文见 [references/failures.md](references/failures.md)；daemon 的行为见 [references/daemon.md](references/daemon.md)（都是英文）。

## 13. 版本与升级

版本就是 git tag `agent-lark/vX.Y.Z`（仓库里有两个 skill，各打各的 tag）；改了什么见 [CHANGELOG.md](../../CHANGELOG.md)。`skills` CLI 与 skills.sh 都不读版本号——装到本机的是仓库内容的一份快照，`npx skills update` 刷新它（全局安装加 `-g`，当前项目加 `-p`）。想停在某个版本，安装时把 tag 当 git ref 带上，按 `skills` CLI 的文档，之后 `update` 会停在那个 ref 上：

```bash
npx skills add 'yezhoujie/agent-remote-communication-skills#agent-lark/v0.1.0' --skill agent-lark
```

**从 0.1.0 升到 0.1.1**：`setup --app-id` 与 `--store`、env 文件（`~/.config/agent-lark/.env`、`AGENT_LARK_ENV_FILE`）、`LARK_APP_ID` / `LARK_APP_SECRET` 这对名字都没了。`setup` 存下的凭据（钥匙串或 `credentials.json`）照常可用；如果你的凭据只写在 env 文件或那对变量里，跑一次 `agent-lark setup --reuse`（§4.1 第 2 种）或导出 `AGENT_LARK_APP_ID` / `AGENT_LARK_APP_SECRET`。选项现在会被检查：命令行里有不认识的选项退 1，不再默默忽略。

**给一台已经跑着 daemon 的机器升级**——按这个顺序：

1. **用你现在手上的 CLI** 停掉正在跑的 daemon：`agent-lark daemon --stop`。有提问挂着时它会拒绝；等回答，或用 `--stop --force`（挂着的卡片变灰）。文件已经换成新版、旧 daemon 又不应答停止请求的话，改用 `kill -TERM <pid>`（pid 在 `~/.agent-lark/daemon.pid` 里）。
2. 换文件：`npx skills update`（或再跑一遍安装命令、或拷目录）。
3. 起新 daemon：`agent-lark daemon --detach`，然后 `agent-lark daemon --status`。每个项目的群都还绑着：新 daemon 照旧读 `bindings.json`。
4. 在 agent 工作的那个 herdr 窗格里跑一次 `agent-lark away on`（或 `ask` / `notify` / `send-file` / `rename` / `bind`），让绑定重新记下这个窗格——`status` 与 `away status` 不记录任何东西。

## 14. 集成方式：让 skill 在整个会话周期里生效

skill 只提供命令——`ask`、`notify`、`send-file`、`away`、`rename`、`unbind`——**有意不规定什么时候用**（SKILL.md「When to use」）。不加约束的 agent 只在碰巧想起这个 skill 时才用它，你离席时靠不住。触发策略要写进 agent 的**常驻指令**（它每个会话都会加载的那份文件），而且要覆盖四个时刻：

1. **会话开始 / 上下文被清空后**：读 `<项目根>/.agent-lark/state.json`（`away status --json`）；`away: true` 就表示人不在、从现在起每个决定都走手机。
2. **人要走了**（「我走了，有事发手机」）：趁他还在键盘旁跑 `away on --name "<任务名>"`，把输出原样转告；退 4 列出旧群时问他复用哪个——绝不自己挑。
3. **离席期间**：每一次提问、确认、授权都变成一张 `ask`（放后台跑、同一时刻只挂一张、按退出码办；`--urgent` 只用于不可逆动作或短超时）；手机来的消息带 `[agent-lark remote] ` 前缀注入会话；`notify` 只用于回答手机上问的问题和**不需要拍板的重大事项**——任务完成、出错、任务无法继续——不用来报进展。
4. **人回来了**：`away off`；任务结束时 `unbind`。daemon 留着。

skill 自带一份照这四条写好的规则：[`examples/remote-mode-rule.zh-CN.md`](examples/remote-mode-rule.zh-CN.md)（中文）、[`examples/remote-mode-rule.md`](examples/remote-mode-rule.md)（英文），也写了多个 agent 会话组队时怎么办（只让对接用户的那个会话持有远程模式）。Claude Code 的 `~/.claude/rules/` 会注入每个会话：

```bash
cp ~/.claude/skills/agent-lark/examples/remote-mode-rule.zh-CN.md ~/.claude/rules/agent-lark-remote-mode.md
```

其他 agent 放到它加载常驻指令的位置。按自己的习惯改开头的 `<skill dir>` 路径和触发用语（「我走了」「我回来了」），其余是产品行为，照写即可。

**两个 skill 都装了？** 它们互不知道对方的存在，也没有任何一方决定一件事该走哪条通路。那是规则的事：每台机器（或每个项目）只让一条规则生效，由它写明调用哪个 CLI。两个 daemon 并排跑没有问题，它们什么都不共享。
