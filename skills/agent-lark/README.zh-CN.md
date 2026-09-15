# agent-lark

[English](README.md) · 中文

本文写给装它的人。agent 读的是 [SKILL.md](SKILL.md) 与 `references/`，你不必向它解释这个工具。它是同一仓库里 `agent-ntfy` 的飞书版；两者的比较见[仓库 README](../../README.md)。

## 目录

1. 它是什么
2. 前提
3. 安装
4. 配置，每台机器一次（4.1 手动 · 4.2 让 agent 帮你 · 4.3 权限清单 · 4.4 凭据）
5. 怎么用：你说什么，agent 做什么
6. 日常（6.1 跟 agent 说的 · 6.2 你自己跑的几条）
7. 出问题了怎么办
8. 安全与边界
9. 升级
10. 让它一直生效：给 agent 的规则

## 1. 它是什么

你的 AI coding agent 碰到一件它自己拿不定的事，而你不在键盘旁。它不干等，把问题推到你手机上、做成一张飞书卡片：每个选项一个按钮、它自己的建议、一行提示。你点一下——或者在群里打一句话——答复就回到提问的那个 agent 会话里，带着全部上下文。你主动发的文字、图片、语音也会到那个会话。不需要服务器、不需要公网地址：本机一个小 daemon 主动出站连飞书；飞书应用用手机扫码就能建，不用企业管理员审核。

```
agent ──提问──▶ daemon ──▶ 飞书 ──▶ 你的手机：点按钮 / 勾选 / 打字 ──▶ 原样回到 agent
```

一个项目（agent 工作的那个目录）一个飞书群；你在哪个群说话，就是在对哪个项目说。

## 2. 前提

- **平台**：macOS 在真机上端到端测过；Linux 与 Windows 只有 CI 单元测试（Windows 原生可跑，shell 示例请走 Git Bash / WSL）——欢迎带着真机报告提 PR。
- Node.js 22 或更新。CLI 是一个自包含单文件（`dist/cli.mjs`）：不用 `npm install`，也不用构建。
- 一个飞书 / Lark 账号——个人版就够——以及跑 daemon 的机器能出站访问飞书。
- **[herdr](https://herdr.dev) 可选**（macOS / Linux 上 `brew install herdr`）。依赖它的只有三件：
  - 提问整条来回（卡片 → 点按钮或打字 → 答复回到 agent）、通知、文件，**不装** herdr 也能用。
  - 你主动发的消息——一句指令、一张图、一条语音、对旧卡片的回复——是由 herdr 打进 agent 终端的；不装就送不到，群里会收到一张回执卡说明。
  - 🔔「等你输入」卡（agent 卡在只有你能回答的提示上时推）也要 herdr；§4.2 里 agent 替你开的那个窗格同样要。

## 3. 安装

装进当前项目（skill 落在 `./.agents/skills/agent-lark`，并从 `./.claude/skills/agent-lark` 打一个符号链接过去；用 `-a <agent>` 只指定一个非 universal 的 agent 时 CLI 会改为拷进那个 agent 自己的目录）：

```bash
npx skills add yezhoujie/agent-remote-communication-skills --skill agent-lark
```

要给所有项目用，加 `-g`：文件放到 `~/.agents/skills/agent-lark`，`~/.claude/skills/agent-lark` 变成指向它的符号链接。**`-g` 的警告**：`~/.claude/skills/agent-lark` 已经是一个真实目录的话，`skills` CLI 会把它删掉换成符号链接——先备份。任何能把 `skills/agent-lark/` 放到 agent 加载 skill 位置的办法都行（`git clone` 后拷目录也一样）；要钉住某个版本，安装时带 git ref（§9）。

CLI 就是目录里的 `dist/cli.mjs`，它管自己叫 `agent-lark`。你很少需要敲它（§6.2），但配个 alias 方便：`alias agent-lark='node "<skills/agent-lark 的路径>/dist/cli.mjs"'`（文件带可执行位，往 `PATH` 里打符号链接也行）。

## 4. 配置，每台机器一次

每台机器只做一次：建一个飞书应用（或复用你已有的），把凭据存起来。别的都不用手动配：群、daemon、开关都是 agent 的活（§5）。两条路都到这里。

### 4.1 手动

```bash
agent-lark setup
```

每一行都中英并排打印（下面只列中文那一半）。在终端里跑，先出菜单：

```
怎么接入飞书？
  1) 扫码新建一个应用（用飞书扫终端里的二维码）
  2) 复用一个已有的应用（输入 App ID 与 App Secret）
选 [1/2]：
```

**1 — 扫码新建应用。** 二维码画在终端里，同一个链接紧跟着以一行文本打出来，画得不好时点链接也行。用手机飞书扫；确认页列出要授权的权限（§4.3）；点同意，应用就建好了。二维码只在几分钟内有效（过期时间会打出来）；过期了重跑 `setup`。成功时：

```
✅ 应用已绑定，凭据保存到：macOS Keychain (service: agent-lark)（明文不会出现在任何输出里）。
下一步：回到 agent 会话，说「开启远程交互模式」或输入 /agent-lark on——daemon 与群绑定由 agent 从它自己的窗格完成，不用你手动跑。
```

**2 — 复用已有的应用**（`agent-lark setup --reuse` 直接进这一支）。它问 App ID（`cli_…`，在开发者后台「凭证与基础信息」里看）和 App Secret——盲输、不回显、之后也不出现在任何输出里，连报错信息里都不会——连一次飞书核这对凭据，存起来，再把这种应用要你**手动开通**的权限、事件、回调列出来（§4.3），最后同样一行「下一步」。飞书不认的凭据会再问一次；连续三次不过就停，什么都不存。

**不管哪条路：** 凭据存进系统钥匙串，没有钥匙串的平台存 `0600` 文件（§4.4）。之后再跑 `setup` 只会说「已经有凭据了（来自 …）」。`setup --update` 重新扫码给同一个应用重新授权（扫码建的应用补权限就靠它）；`setup --reset` 先删掉存着的凭据，所以 `agent-lark setup --reset --reuse`（或只 `--reset`，走扫码）就是换一个应用。

### 4.2 让 agent 帮你

对 agent 输入 `/agent-lark setup`（或直接说「配置一下 agent-lark」/「开启远程交互模式」——还没有凭据时它会先走配置）。它先问你走哪条路，不替你选：

- **扫码新建应用**：agent 跑 setup，把链接（或二维码图片）给你；你用飞书扫，它报结果。
- **复用已有的应用**：App ID、尤其是 App Secret 不能经 agent 之手。在 [herdr](https://herdr.dev) 里，agent 会在它自己下方新开一个终端窗格、焦点切过去：在那个窗格里输 App ID 和 Secret；你做完，agent 自己会拿到结果。不在 herdr 里，agent 会把一条命令给你，**在你自己的终端窗口**（Terminal / iTerm 等）里跑——不是在 agent 会话里；跑完告诉 agent 一声。

两条路都只存凭据，agent 报个结果；开启远程模式是你接下来说的那句话（§5）。底下的交接怎么做，见 [SKILL.md](SKILL.md)「Invoked with an argument」。

### 4.3 权限清单

扫码建应用时确认页申请的就是这些；复用的应用要你在开发者后台（应用 → 权限管理）手动开通同一批，外加事件 `im.message.receive_v1` 与卡片回调 `card.action.trigger`，两者都选「使用长连接接收」——然后发布一个版本，否则不生效：

```
im:message   im:message:send_as_bot   im:message.group_msg   im:chat   im:resource   im:message.urgent   speech_to_text:speech
```

`im:message.urgent` 是加急要的；`speech_to_text:speech` 是语音转文字要的（只有付费租户可用，§7）。扫码建的应用之后缺了哪个？`agent-lark setup --update` 补上。

### 4.4 凭据

三个来源，高到低：环境变量 `AGENT_LARK_APP_ID` / `AGENT_LARK_APP_SECRET`（运行时覆盖）、系统钥匙串（`setup` 默认写这里：macOS `security`、Linux `secret-tool`、Windows 上是 DPAPI 加密文件）、`~/.config/agent-lark/credentials.json`（权限 `0600`）。别的一概不读——没有 env 文件。`agent-lark status` 会标出用的是哪一个，**但不打印值**。细节与全部环境变量：[references/daemon.md](references/daemon.md) §7。

## 5. 怎么用：你说什么，agent 做什么

**开启。** 说「开启远程交互模式」/「我走了，有事发手机」，或输入 `/agent-lark on`。agent 从它自己的终端窗格跑 `away on --name "<任务名>"`（你手机上的消息以后就打进这个窗格）：daemon 没跑就起，飞书里建一个叫 `<任务名> [<项目目录名>]` 的群、把你拉进去，开关打开。**这个项目以前用过群的话**（上一个任务结束了，或本地记录丢了），agent 不会再建一个：它把旧群列出来**问你**拿回哪一个——改成新任务名——还是新建；不替你选。不在 herdr 里时它还会告诉你：手机上发的消息不会打进它的会话。

**离开期间——手机上会看到什么。**

- **一张提问**是蓝色卡片，标题「🤔 [<项目目录名>] <标题>」：agent 在做什么、背景、卡在哪、编号的选项（推荐项有标记）、它的理由，然后每个选项一个按钮。点按钮，或者**直接在群里打字**——提问挂着的时候，你在群里发的第一条消息**就是**答复，不管内容是什么。卡片变绿（「✅ … · 已回答」），你的回复在最上面。没人回答 ⇒ 超时后变灰（「⌛ … · 已超时」，默认 12 小时），agent 放弃时也变灰（「⚠️ … · 已取消」）。三个变体：答案可能是好几项时是一组复选框加**提交**按钮；不可逆的选项是红色按钮加二次确认（agent 永远不能推荐这种选项）；agent 标了加急时卡头是红色、并在飞书里加急提醒你。
- **一条通知**是浅蓝色的「📣」卡片，没有按钮、没有状态：永远不变色，回复它就是一条普通指令。agent 也可能往群里发一张图或一个文件。
- **你主动发的任何内容**——没有提问挂着时——会作为一句指令打进 agent 的会话，前缀 `[agent-lark remote] `；送到后你那条消息会被贴上 `Get` 表情。图片和文件会存到机器上、把路径给 agent。语音只在飞书付费租户上会转成文字（§7），但不管怎样都会保存。用飞书的「回复」功能回某张卡片，agent 会知道你指的是哪张。
- **🔔「[<项目目录名>] 等你输入」**（橙色；只在 herdr 里有）表示 agent 卡在只有你能回答的提示上——权限确认、选择题——它会一直等到你回到键盘旁。
- 提问是 agent 写的 JSON（契约在 [SKILL.md](SKILL.md)）；你永远不用写。

**回来。** 说「我回来了」或输入 `/agent-lark off`：agent 跑 `away off`——只关开关，群和 daemon 都留着。任务结束时 agent 先问你群留不留：留 ⇒ `unbind`（群留在飞书里，这个项目下次开启远程模式时 agent 会把它提出来）；不留 ⇒ `unbind --dissolve`（解散群、忘掉记录）。远程模式只换通道，不降标准：不可逆动作仍要你明确批准，超时不算批准。

## 6. 日常

### 6.1 跟 agent 说的

| 你说 | agent 跑 |
|---|---|
| 「任务换了，叫 X」 | `rename "X"`——群名变成 `X [<项目目录名>]` |
| 任务结束 | agent 先问群留不留：「留」⇒ `unbind`——群留在飞书里，下次提出来；「不留」⇒ `unbind --dissolve`——在飞书里解散并忘掉（飞书拒绝解散时 agent 会让你手动解散；本地记录两种情况都删） |
| 「用群 oc_xxxxxxxx」（你自己建的，或重装之后） | `bind --chat oc_xxxxxxxx`——群描述被改写成标记本项目 |
| 「开启 / 关闭远程交互模式」 | `away on --name "…"` / `away off`（§5） |

### 6.2 你自己跑的几条

```bash
agent-lark status            # 凭据（哪一层，不打印值）、herdr、daemon、每个项目的群
agent-lark daemon --status   # daemon: pid …  connected true  connection connected  pending questions 0  bound projects 1  started …
                             # media: ttl 7 days, … MB in … files (as of last sweep …)
agent-lark daemon --stop     # 升级前（§9）；有提问挂着时拒绝——--stop --force 取消提问并停掉
```

`agent-lark --help` 列出其余全部命令；那些是 agent 用的（SKILL.md）。换任务、换群、上下文重置都不用停 daemon；它同时管着所有项目。**删掉飞书应用**：`setup` 建的应用是你租户里真实的自建应用——先在飞书管理后台*停用*它（工作台管理 → 应用管理），再到开发者后台删除，换应用时再 `agent-lark setup --reset`（或 `--reset --reuse`）。

## 7. 出问题了怎么办

- **卡片没来，agent 说退出码 3。** daemon 没跑，或连不上飞书：`agent-lark daemon --status` 显示最后一次错误（凭据不对、没网）；daemon 会自己不断重连，排除原因后让 agent 再试。根本没在跑 ⇒ agent 会起它；你也可以：`agent-lark daemon --detach`。
- **卡片上显示「已读 0/0」。** 那是飞书对机器人消息的已读计数，不是送达状态。真正算数的标记：提问被回答会变绿；你主动发的消息送到 agent 后会被贴上 `Get` 表情。
- **你发的消息收到一张「没能送达」回执卡。** 原因写在卡上：那台机器没有 herdr、agent 的窗格没了、或 agent 正卡在只有你能回答的提示上。让 agent 重新开一次远程模式会重新记录它的窗格；最后一种情况回到电脑前处理那个提示。
- **语音存下来了但没转成文字。** 转写需要 `speech_to_text:speech` 权限**且飞书付费租户**；免费 / 个人版租户即使已开通权限，飞书也会拒绝（HTTP 400，code 99991400）。改打字。语音控制在一分钟内。
- **二维码过期了。** 重跑 `setup`（手动，或再让 agent 来一次）。
- **复用的应用发不出东西 / 建群报权限错误。** 权限、事件、回调没开，或没发布版本：按 §4.3 做完再试。
- **agent 给了你一条 `setup --reuse` 命令。** 你不在 herdr 里：在自己的终端窗口跑，不要在 agent 会话里跑（输 secret 需要真终端），跑完告诉 agent。
- **旧群一直被提出来。** 只要它还在飞书里就会被提出来：让 agent 新建一个、把旧的丢掉（绑定着的时候跑 `unbind --dissolve`），或者你自己在飞书里解散它——daemon 会忘掉已经不存在的群（每天一次，以及每次找旧群提出来的时候）。应用解散不了的群（它不是群主）留给你手动解散。

每个退出码的 stderr 原文与 agent 被要求怎么办：[references/failures.md](references/failures.md)。

## 8. 安全与边界

- 凭据存在系统钥匙串或 `0600` 文件里；App Secret 由你在交互式 `setup` 里自己输入，不经 agent、不进 argv、不进文件、不进任何输出（§4.4）。
- **群里的任何人都能操纵你的 agent**：点一下就是答复，打一句话就是指令（有 herdr 时）。建群时群里只有你；保持这样。通路不过滤内容。
- 内容经过飞书服务器——提问描述你的项目，图片与文件从飞书下载——群描述带着你项目的绝对路径（群就是靠它找回来的）。别往提问里放密钥。
- agent 只能发项目内、daemon 媒体目录或临时目录里的文件；daemon 日志只记 id 和事件，不记消息文字。
- 一个项目同时只挂一个提问；一个自建应用只服务一个租户，一台机器只存一套凭据。
- 卡片各字段上限、60 字符的任务名、手机上的样子：[references/message-spec.md](references/message-spec.md)。文件在哪、媒体目录及其 7 天保留期、项目级状态文件：[references/daemon.md](references/daemon.md)。
- Linux 与 Windows 只有单元测试覆盖，没有在真实环境里跑过端到端（§2）。

## 9. 升级

版本就是 git tag `agent-lark/vX.Y.Z`；改了什么见 [CHANGELOG.md](../../CHANGELOG.md)。装到本机的是仓库内容的一份快照，`npx skills update` 刷新它（全局安装加 `-g`，当前项目加 `-p`）。想停在某个版本，安装时把 tag 当 git ref 带上：`npx skills add 'yezhoujie/agent-remote-communication-skills#agent-lark/v0.1.2' --skill agent-lark`。

跑着 daemon 的机器：1. 用手上的 CLI `agent-lark daemon --stop`（有提问挂着时会拒绝——等一等，或 `--stop --force`；文件已经换成新版、旧 daemon 又不应答的话，`kill -TERM <pid>`，pid 在 `~/.agent-lark/daemon.pid` 里）。2. 换文件。3. `agent-lark daemon --detach`。凭据、群绑定、项目级开关都会保留；然后说一声「开启远程交互模式」，让 agent 重新记下它的窗格。

## 10. 让它一直生效：给 agent 的规则

skill 只提供命令，**有意不规定什么时候用**。不加约束的 agent 只在碰巧想起这个 skill 时才用它。触发策略要写进 agent 的**常驻指令**，而且要覆盖四个时刻：会话开始时读项目的 `state.json`（`away: true` ⇒ 你不在）· 你要走时它趁你还在跑 `away on`、有旧群时问你 · 你不在时每个决定都变成一张提问卡，`notify` 只用于重大事项、不报进展 · 你回来时 `away off`，任务结束时 agent 问你群留不留，再跑 `unbind` 或 `unbind --dissolve`。

skill 自带一份照这四条写好的规则——[`examples/remote-mode-rule.zh-CN.md`](examples/remote-mode-rule.zh-CN.md)（中文）、[`examples/remote-mode-rule.md`](examples/remote-mode-rule.md)（英文），也写了多个 agent 会话组队时怎么办。Claude Code 的 `~/.claude/rules/` 会注入每个会话：

```bash
cp ~/.claude/skills/agent-lark/examples/remote-mode-rule.zh-CN.md ~/.claude/rules/agent-lark-remote-mode.md
```

其他 agent 放到它加载常驻指令的位置；按自己的习惯改开头的 `<skill dir>` 路径和触发用语。**两个 skill 都装了？** 它们互不知道对方：每台机器（或每个项目）只让一条规则生效，由它写明调用哪个 CLI；两个 daemon 可以并存。
