# 远程交互模式（agent-lark）：用户离席时，要拍板的事推到手机

> 这是一份**给 agent 读的常驻规则示例**。skill 本身只提供通路（`ask` / `notify` / `send-file` / `away` / `rename` / `unbind`），
> 不规定什么时候用；什么时候用由这样一条规则定。Claude Code 用户把本文件复制到 `~/.claude/rules/`（每个会话自动注入）即可；
> 其他 agent 放到它常驻加载的位置。英文版见 `remote-mode-rule.md`。
>
> 下文 `$AL` = `node <skill dir>/dist/cli.mjs`；Claude Code 全局安装时 `<skill dir>` 是 `~/.claude/skills/agent-lark`。怎么写一张提问卡看 SKILL.md。

## 状态在哪：`<项目根>/.agent-lark/state.json`（CLI 写，你只读）
- **会话开始 / 上下文被清空或重置后**，先 `$AL away status --json`：`away: true` ⇒ 本项目已在远程模式，照「模式内」办；文件不存在或 `false` ⇒ 正常终端交互。
- 字段：`away`（开关）· `chatId`（本项目绑定的飞书群，`null` = 没有）· `target`（项目根）· `updated`。**里面永远没有凭据**，注入目标窗格也不在里面——那个记在 daemon 那边。

## 开 / 关（用户的话就是开关）
- **开启**：「开启远程交互模式」「我走了，有事发手机」「切到手机」「remote on」——用户对你输入 `/agent-lark on` 也是同一个意思（见 SKILL.md「Invoked with an argument」）。用户此刻还在键盘旁，**当场做三件**（选群、扫码只有人在时才做得了）：
  1. **从自己的终端窗格**跑 `$AL away on --name "<任务名>"`——一站式：daemon 没跑就起、等它连上飞书、把本项目绑到一个叫 `<任务名> [<目录名>]` 的群，然后才写开关。**stdout 原样转告用户。** 结果：
     - rc 0 ⇒ 完成。群那一行说明是哪个群（`Created Feishu group "…"` / `Took back Feishu group "…"` / `Connected to Feishu group "…"`）；开关行 `Remote mode is on: …` 在 herdr 内是最后一行；herdr 外它后面还跟一行 `Not inside herdr: …`（手机消息不注入、无卡住提醒）。
     - rc 4 `No Feishu app credentials yet` ⇒ **先问用户**：扫码新建一个应用，还是复用已有的应用（他知道 App ID 与 App Secret）。扫码 ⇒ 替他后台跑 `$AL setup </dev/null`，把二维码下面那行 URL 交给他（或渲成 PNG 打开）。复用 ⇒ 跑 `$AL setup --reuse`：在 herdr 里它会在你下方开一个窗格让用户自己输 App ID 与 App Secret（secret 不经你的手），用户做完后你的会话里会收到一行 `[agent-lark] setup:` 开头的结果——等它；在 herdr 外它退 4、把完整命令打在 stderr——把那条命令给用户在他自己的终端跑，跑完让他说一声。必须在用户自己的终端窗口（Terminal / iTerm 等）里跑。不许建议在本会话内执行——`!` 开头的命令、shell 工具、后台任务都没有 TTY，CLI 会直接拒绝。然后再 `away on` 一次。（`/agent-lark setup` 走的就是这条流程；见 SKILL.md「Invoked with an argument」。）
     - rc 4 `this project has no live group, but N earlier group(s) could be taken back` ⇒ 把 stderr 里的候选列表（一行一个群：名字、何时解绑、群 id）转告用户，**问他改名复用哪一个、还是新建**——不要替他选；带 `--reuse <chatId>` 或 `--new` 重跑。
     - rc 3 ⇒ **没开启**，什么都没写。先 `$AL daemon --status`；daemon 没跑就 `$AL daemon --detach` 再 `away on` 一次；仍 3（飞书连不上，`daemon is up but not connected to Feishu: …`）⇒ 停下，把 stderr 转告用户。
  2. `$AL away status --json` 核 `away: true`（`away on` 顺带登记了你的窗格为注入目标——**只在自己的窗格跑**）。
  3. 回一句 stdout 的结论。
- **关闭**：「关闭远程交互模式」「我回来了」「remote off」，或 `/agent-lark off` ⇒ 走「关闭与收尾」。

## 模式内
- **一切本来要向用户提问、要他确认或授权的时刻** ⇒ 用 `$AL ask`，不再在终端等。
- `ask` 会阻塞到用户回答。你的 shell 工具有执行时限的话（Claude Code 的 Bash 工具是 10 分钟），**放后台跑并把 stdout / stderr 重定向到文件**——前台被杀 daemon 会视为取消、卡片作废；收到完成通知再读文件取回复与退出码。**同一项目同一时刻只挂一个 `ask`**（第二个退 4）。
- 退出码：0 按回复办；1 改 JSON 重发；2 超时 ⇒ 可逆的事按推荐项继续并记「未获确认」，不可逆的停下等人；3 通道故障 ⇒ `$AL daemon --status`，没跑就 `--detach` 起，重发一次，仍 3 停下；4 需要人（未绑定 / 已有提问挂着）⇒ 停下等用户回终端。
- **`--urgent` 只用于不可逆动作或短超时**（它会在应用内加急提醒 owner；每张都加急等于没有加急）。不可逆选项标 `"danger": true`（红色按钮 + 二次确认），且永远不能是推荐项——校验会直接拦下。
- **`"select": "multi"` 只在答案可能同时是好几项时用**（「这几项检查跑哪些」）；是 / 否、多选一的问题保持单选——一下点完，不用勾好几个。
- **手机来的消息以 `[agent-lark remote] ` 前缀注入你的会话**（**只在 herdr 内**；herdr 外没有注入，他主动发的消息只会收到「Not delivered」回执卡，`ask` 的回复照常回到调用），按用户输入处理。图片、文件带 `[saved: <路径>]` 行（从那里打开）；语音以转写文字到达。
- **手机消息算哪道题的回复**：你有 `ask` 挂着时，用户发的**任何**消息都算它的回复，哪怕是引用回复别的卡。没挂着时，引用回复你某张卡的消息首行带 `(reply to: "<那张卡的标题>")`——「对，就这么办」指的就是它；没引用的就是普通指令。在已结束的卡上迟到点击会以 `(follow-up) I pick <label>` 到达；以最后一条为准。
- **看到前缀就说明人在手机上**：照常在终端回答，同时用 `notify` 把同一个答复推过去。
- **`$AL notify`（单向通知：stdin JSON `{title, body, lang}`，不阻塞、无按钮，提问挂着时也能发）只在两种情况下用**：① 用户从手机问了只要回话的问题（「进展如何」）——回答放 body；② **重大事项、必须让用户知道而不需要他拍板**：任务完成 / 发生异常错误 / 任务无法继续（含 `ask` 退 3、4 后停下等人）。其余进展、中间结果、顺带一提**一律不发**——每张卡都会响手机。要拍板的事永远走 `ask`，不用 notify 代替。
- **`$AL send-file <路径> --caption "…"` 用来省一次往返**：用户要看版式、看 diff、看产物时，直接把文件发过去（截图、渲染出的页面、构建产物），不要先描述再等他来要。只能发项目内、daemon 媒体目录、临时目录里的文件。
- 需要用户验证某个改动（版式 / 文案）时不单发验证卡——塞进下一张本来就要发的真问题卡（「这张卡顺便看 X」），或直接 `send-file` 那个东西。
- **🔔「等你输入」卡是自动的**（herdr 内、远程模式开着时）：herdr 报你的会话卡在只有人能答的提示上，daemon 自己推。不要手发，也不要用 `notify` 说自己卡住了——还能跑命令就不算卡住。
- 远程模式只换通道，**不降标准**：不可逆动作仍要明确批准，超时不算批准。
- 用户在终端直接回话（没有 `[agent-lark remote] ` 前缀）而手机上还挂着一张 `ask` ⇒ 先停掉那个后台任务（Claude Code 里是 TaskStop；卡片会变「Cancelled」），再按终端回话办；不要两边都等。
- **任务换了**（同一项目里用户交了别的活）⇒ `$AL rename "<新任务名>"`，让手机上的群名说得清这是在做什么。

## 多个 agent 会话组队时（一个主会话派活给别的会话）
- **只有对接用户的那个会话（主会话）持有远程模式**：跑 `away on`、发 `ask` / `notify` / `send-file`、读 `state.json`。其他会话照旧只对接主会话，不调 `ask`。
- 别的会话需要用户授权 / 拍板的事 ⇒ 由主会话用 `ask` 问用户，拿到答复后按你们既有的通道转达。
- 手机来的消息注入的是主会话（绑定上记的窗格），带 `[agent-lark remote] ` 前缀 ⇒ 当用户输入。
- 状态以 `state.json` 为准，主会话重置后照第一节重读即可；队伍自己的状态文件里写一句「远程模式见 state.json」就够，别另记一份。
- 你们若有「自动驾驶 / 不必逐项请示」之类的授权，它与远程模式正交：前者管哪些事不必问，后者管必须问的走哪条通道。

## 关闭与收尾（缺一步都不报错）
- 关闭模式：先确认没有挂着的后台 `ask`（有 ⇒ 等它或停掉，卡片会变「Cancelled」）→ `$AL away off`（只关开关；群与绑定留着，daemon 留着不停）。
- **任务结束时提醒用户并跑 `$AL unbind`**：群是他的（留在飞书里，要不要归档是他的事），下次同目录 `away on` 会问要不要改名复用。模式仍开着（用户没回来）就留着开关，提醒改用 `notify` 发。
- 手机上的订阅、点按钮、扫码等一切动作都是用户的，agent 不代做、也做不了。

## 禁止
- 不问就跑 `setup`、替用户选路，都不行（它会用他的账号建或绑飞书应用）：先问是扫码新建应用、还是复用已有的应用；用户选定后可以替他跑 `setup`（扫码：把二维码的 URL 行交给他，或自己渲成 PNG 打开）或 `setup --reuse`（herdr 窗格，或把命令给他）。App Secret 只在交互式 `setup` 里由用户自己输入，不经 agent 之手、不进 argv、不进你写的任何文件、不进任何输出。
- 不用自己的后台 shell 去起 daemon 常驻（用 `daemon --detach`）；有提问挂着时不 `daemon --stop --force`；不手改 `state.json` / `bindings.json`，只经 `away` / `bind` / `unbind` / `rename`。
- 不 `bind --chat` 别的项目的群；不复用用户没选过的群。

## 两个 skill 都装了（agent-ntfy 与 agent-lark）
两个 skill 互不知道对方，也都不决定一件事该走哪边。**每台机器（或每个项目）只让一条远程模式规则生效**，由它点名调用哪个 CLI：agent-lark 用本文件，ntfy 用 `agent-ntfy` 的 `examples/remote-mode-rule.md`。想按项目分流就两条都留，各自开头加一句「本规则只在项目有 `.agent-lark/state.json` 时适用」/「…`.agent-ntfy/state.json`…」，由对应 CLI 的 `away status --json` 决定。两个 daemon 可以并存，互不相干。
