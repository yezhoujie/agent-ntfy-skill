# agent-ntfy

[English](README.md) · 中文

[![skills.sh](https://skills.sh/b/yezhoujie/agent-ntfy-skill)](https://skills.sh/yezhoujie/agent-ntfy-skill)

让任意 AI coding CLI 把它自己拿不定的事经 [ntfy](https://ntfy.sh) 推到你的手机，再把你的裁决——或任何一句指令——直接送回 agent 的会话；也能往同一部手机推单向通知。
不需要服务器、不需要固定 IP、不需要付费服务，除 Python 3 外零依赖。

本文写给装它的人。agent 读的是 [SKILL.md](SKILL.md) 与 `references/`，你不必向它解释这个工具。

## 目录

1. 工作原理
2. 前提（2.1 平台支持 · 2.2 iPhone 用网页版）
3. 安装
4. 你要动手的只有两件事
5. 第一条提问，从头到尾
6. 手机不弹通知怎么办（Android / MIUI 排查清单）
7. 安全须知
8. 已知边界（8.1 支持哪些 agent CLI）
9. 环境变量
9.1 远程模式与项目级状态文件
10. 已知行为
11. CLI 参考
12. 版本与升级
13. 集成方式：让 skill 在整个会话周期里生效

## 1. 工作原理

```
agent ──ask（stdin 里的 JSON）──▶ agent-ntfy ──本机 socket──▶ daemon ──HTTPS──▶ ntfy.sh ──▶ 你的手机
      ◀── 回复经 stdout 返回 ───            ◀────────────────        ◀── SSE ────         ◀── 点按钮 / 打字
                                                                        │
                                                              没有提问在等？
                                                                        ▼
                                                         注入到 agent 所在的 herdr 窗格
```

- agent 交来一段含 8 个必填字段的 JSON；CLI 把它渲染成固定版式的 Markdown 卡片、**只带一个按钮**（「采纳推荐」）并推送。agent 阻塞等你点按钮或打字，你的回复原样返回给它。它也可以用 `notify` 推一张单向卡片（标题 + 正文，无按钮）然后接着干活。
- 常驻 **daemon** 是唯一的 ntfy 订阅者。没有提问在等的时候你发的任何内容，会带着 `[agent-ntfy remote] ` 前缀作为一句指令注入 agent 的会话（只有这一步需要 herdr；不装时哪些能用见 §2）。
- 每个**项目**（agent 工作目录所在的 git 仓根，不在仓里就是那个目录）租用一个**槽位** = 池子里的一个随机 ntfy topic，池子存在钥匙串、DPAPI 文件或 0600 文件里（§7）。回复按 topic 路由。租约还记着这个项目最近一次跑 `ask`、`notify`、`slots`、`release`（不带参数）、`away on` 或 `away status` 的 herdr 窗格，手机消息就注入到它。
- 通路只搬运文字：不解释、不代答、不去重。
- 本机 socket 在 macOS / Linux 上是 unix socket，在 Windows 上是回环 TCP 端口（§9 `AGENT_NTFY_IPC`）。

## 2. 前提

- Python 3.10 或更新——只用标准库，不用 `pip install` 任何东西
- 手机上装 [ntfy app](https://ntfy.sh)；不需要 ntfy.sh 账号。实测环境是 Android（MIUI）。**iPhone 用户看 §2.2**——iOS 版 app 只能收通知、没有输入框，回话要用网页版
- 能出站 HTTPS 访问 `ntfy.sh`（或你自建的实例）。daemon **不读** `http_proxy` / `https_proxy` 与系统代理；对进程透明的 TUN 型 VPN 可以
- 本文与 SKILL.md 里的示例是 POSIX shell 形态（`$(...)`、heredoc、`alias`）。Windows 上请在 Git Bash 或 WSL 里跑这些命令；daemon 与 CLI 本身原生可跑（那里的解释器通常叫 `python` 而不是 `python3`）
- [herdr](https://herdr.dev)，推荐——见下

### 2.1 平台支持

| 平台 | 状态 |
|---|---|
| macOS | 全链路在真机上实测过：daemon、`ask` / `notify`、herdr 注入、真手机上的真卡片。topic 池存钥匙串 |
| Linux | **只有 CI 单元测试**（ubuntu-latest，Python 3.10 与 3.13）。没有做端到端真实环境测试——欢迎提 PR。topic 池存 `0600` 文件；herdr 有 Linux 版 |
| Windows 10 / 11 | **只有 CI 单元测试**（windows-latest，Python 3.10 与 3.13，含真实的 DPAPI 加解密往返、回环 TCP 传输与真实起停的后台 daemon）。没有做端到端真实环境测试——欢迎提 PR。原生可跑；shell 示例请走 Git Bash / WSL。经 herdr 的手机 → agent 注入在 Windows 上未验证：herdr 窗格里的 shell 怎么拆命令行没有覆盖，而 CLI 是用 `subprocess.list2cmdline` 拼窗格命令行的（cmd.exe / MS C 运行时的引号规则）。读线程阻塞时停 daemon 或重订阅在 Windows 上多付 0.2 秒¹ |

¹ Windows 上 `shutdown()` 叫不醒阻塞在 `recv()` 的线程，daemon 等它 0.2 秒后才真正关掉 socket。

### herdr：能装就先装；不装的话哪些能用、哪些不能

[herdr](https://herdr.dev) 是本 skill 用来把文字送*进* agent 会话的终端复用器（macOS / Linux 上 `brew install herdr`；其他平台见 https://herdr.dev）。它是唯一可选的一环，不装时留下什么、失去什么如下：

| 不装 herdr 也能用 | 必须有 herdr |
|---|---|
| `ask` 整条链路：手机上出卡片 → 点按钮或打字 → 回复回到 stdout → 退出码；`notify` | 手机 → agent 的消息：没有提问在等时你主动发的指令，或者回一张已经超时 / 已取消的旧卡片 |
| daemon 与其余全部子命令：`confirm-sub`、`slots`、`release`、`add-slot`、`away` | `away on` 在窗格里起 daemon、`confirm-sub` 替你开窗格（没有 herdr 时它们退回 `daemon --detach` 与「请你自己跑 `confirm-sub`」） |
| 租约两种情况下都按项目算；不装 herdr 时租约上没有窗格，也就没有可注入的地方 | |

这种消息不会静默丢掉。daemon 会在手机上回一张回执，标题「[slotN] 消息未送达」，正文是「槽位 slotN 的租约没有登记目标窗格（发起命令的会话不在 herdr 里）。在 herdr 窗格里对这个项目跑 ask / notify / slots / release（不带参数）/ away on 或 away status 任一条即可登记，或释放这个槽位。」加一行「你刚才发的内容没有送达任何 agent。」，带「释放这个槽位」/「忽略」两个按钮。

为什么非 herdr 不可：注入就是往目标 agent 的终端（PTY）里写一行文本，`herdr agent prompt` 是对任何 agent CLI 都通用的唯一办法；本 skill 没有别的兜底机制。

### 2.2 iPhone：用 ntfy 网页版，不用 App Store 里的 app

ntfy 的 iOS app 能收通知，但**没有输入框**：不能在 topic 里打字，于是既不能用按钮以外的方式回答提问，也不能主动给 agent
发指令。改用网页版、加到主屏幕当 app 用（本 skill 的一位 iPhone 用户实测的做法；作者本人未测）：

1. 在 iPhone 的 **Safari** 里打开 `https://ntfy.sh/app`。
2. 点底部的**分享**按钮，选**添加到主屏幕**。
3. 之后**从桌面那个 ntfy 图标打开**，不要继续用 Safari 标签页。
4. 首次打开时系统会请求通知权限——必须点**允许**。
5. 在里面订阅 topic（确认窗格里显示的那个），并从这个 app 完成可达性确认（§4）。提问以带按钮的通知到达；topic 底部的输入框
   用来发自由回复和指令。

Android 用户照常用 ntfy app，它有输入框。

## 3. 安装

装进当前项目（缺省 skill 落在 `./.agents/skills/agent-ntfy`，并从 `./.claude/skills/agent-ntfy` 打一个符号链接过去；用 `-a <agent>` 只指定一个非 universal 的 agent 时 CLI 会改为拷进那个 agent 自己的目录）：

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy
```

要给所有项目用，加 `-g`：文件放到 `~/.agents/skills/agent-ntfy`，`~/.claude/skills/agent-ntfy` 变成指向它的符号链接。

> **`-g` 的警告。** 如果 `~/.claude/skills/agent-ntfy` 已经是一个真实目录（你手动拷进去的副本），`skills` CLI 会把它删掉、换成符号链接。先备份。（这是读 CLI 源码得出的，没有在真实目录上试过。）

任何能把 `skills/agent-ntfy/` 放到 agent 加载 skill 位置的办法都行（`git clone` 后拷目录也一样）。要钉住某个版本，安装时带 git ref：`npx skills add 'yezhoujie/agent-ntfy-skill#v0.1.0' --skill agent-ntfy`（§12）。

CLI 就是目录里的 `scripts/agent_ntfy.py`。它自己的提示文案里管自己叫 `agent-ntfy`；配一个 alias 下面的命令会短很多：

```bash
alias agent-ntfy='python3 "<skills/agent-ntfy 的路径>/scripts/agent_ntfy.py"'
```

## 4. 你要动手的只有两件事

1. 装 skill（上面）。一次。
2. 某个槽位第一次被用到时，在手机上订阅它的 topic，并在测试通知上点按钮（§5 第 2 步）。每个槽位一次。

其余全自动：topic 池首次使用时自动生成，daemon 可由 agent 代劳启动，租约按需分配。

## 5. 第一条提问，从头到尾

**第 1 步——起 daemon。** 它必须比 agent 活得久，所以自己单独跑：

```bash
agent-ntfy daemon --detach        # 任何平台：daemon：已在后台启动，pid 12345（日志 ~/.agent-ntfy/daemon.log）
agent-ntfy daemon                 # 在 herdr 里：改在一个空闲窗格里前台跑，看得见
agent-ntfy daemon --status        # daemon：pid 12345  订阅：已连上  等待中的提问：0  确认中：0  槽位：5  传输：unix
```

这里的路径用 `~` 缩写；CLI 打印的是展开后的绝对路径。`--detach` 若报「daemon（pid 12345）5 秒内还没就绪，仍在启动；稍后用 agent-ntfy daemon --status 看，日志 …」，先看日志——macOS 上可能的原因之一是首次运行时屏幕上有钥匙串授权对话框（池子是经 `security` 命令读的）：答完再跑 `--status`。要看到中文文案，加 `--lang zh`（或设 `AGENT_NTFY_LANG=zh`，或 shell 本身就是中文 locale）：daemon 的语言以启动时解析的为准，之后不再改（由 `away on` 代起时，它会把调用方的语言带过去）。绝不要把 daemon 当作 agent 自己 shell 的后台任务起：agent 一退出它就没了。这一步也可以不手动做：`away on`（§9.1）发现没有 daemon 应答时会自己起一个。

**第 2 步——确认手机收得到槽位 1 的通知。** 在你自己的终端跑（别经 agent：它会打印 topic 名，那就是密码）：

```
$ agent-ntfy confirm-sub slot1
slot1 的 topic：agent-ntfy-xxxxxxxxxxxxxxxxxxxx
订阅地址：https://ntfy.sh/agent-ntfy-xxxxxxxxxxxxxxxxxxxx
在手机 ntfy app 里订阅上面这个 topic；订阅好后按回车，我会发一条带按钮的测试通知——看到它弹出来、点按钮，确认就完成了。
⚠️ 按回车、点按钮之前别关这个窗格 / 终端：关了确认就取消，要重来。
订阅好了就按回车…
agent-ntfy: 测试通知已发出，请在手机通知栏点「我收到了」（600 秒内）…
✅ slot1 已确认：手机收得到通知，之后 agent 可以用它提问了
这个终端窗口可以关了。回到你的 agent 会话，把下面这句发给它：
  agent-ntfy：slot1 已过闸，可以用它提问了
```

只有点按钮算数，而且要点**弹出来的那条通知**——在 app 里点证明不了通知会弹（见 §6）。10 分钟内没弹出来命令退出 2；把手机设置修好再跑一次。「✅ … 已确认」之后命令会打「这个终端窗口可以关了。回到你的 agent 会话，把下面这句发给它：agent-ntfy：slot1 已过闸，可以用它提问了」——把那句发给 agent；你自己跑的确认，agent 没有别的办法知道结果。agent 自己在 herdr 里跑 `confirm-sub` 时，它会给你新开一个窗格、里面就是上面这段对话，并告诉你看哪个窗格；topic 名不会进 agent 的输出。按回车、点按钮之前别关那个窗格——关了确认就取消。确认结束时窗格会自己把结果送回 agent（agent 会话里出现一行 `[agent-ntfy] ` 开头的话），你不用转达；看到「✅ … 已确认」后它会问「关闭这个窗格？[Y/n]」：回车关掉，`n` 保留。

**第 3 步——先问自己一个问题**，看一遍来回：

```bash
agent-ntfy ask <<'JSON'
{
  "title":       "测试：吃什么甜点",
  "doing":       "验证 agent-ntfy 能到达这台手机",
  "description": "这是这台机器经 agent-ntfy 发出的第一条提问，答什么都没有影响。",
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

手机上出现卡片：标题「[<项目目录名>] 测试：吃什么甜点」，然后是加粗的分段标记（【正在做】【背景】【卡点】【选项】【我的建议】【要你定】）、一条横线、末尾提示和一个按钮。选项在 Markdown 源码里写成「1\. 蛋糕（推荐）→ …」「2\. 派 → …」并用空行隔开——点号加了转义，CommonMark 会把它渲染成普通的「1.」，因为 ntfy 的 Android app 会把真正的有序列表渲染成圆点、编号就没了（不渲染 Markdown 的客户端会看到那个反斜杠）。点 **采纳推荐**，终端打印 `蛋糕`；改在 app 的输入框里打「当然是派」，终端就打印 `当然是派`。手机上那张卡片会变成「✅ 已回复 · …」，你的回复在上面、原提问保留在下面。

租约归项目所有（§1）。如果你就是在 agent 将要工作的目录里跑的这次测试，agent 会直接复用 slot1——什么都不用做。如果是在别处跑的，在那里跑一次 `agent-ntfy release`（不带参数就释放当前项目租的槽位）；否则 agent 会拿到 slot2——未确认——再把你拉回第 2 步。

**第 4 步——交给 agent。** 它自己读 SKILL.md。碰到还没确认过的槽位时，它会以退出码 4 失败并请你去跑 `confirm-sub slotN`（第 2 步）——在 herdr 里则是直接替你把那个窗格开好。这是设计，不是 bug：topic 名不能经过 agent 的输出。

**通知。** agent 也可以发一张不需要回答的单向卡片：

```bash
agent-ntfy notify <<'JSON'
{"title": "构建完成", "body": "**测试**：483 条通过。\n\n没有要拍板的事，只是告诉你一声。", "lang": "zh"}
JSON
```

它打印「通知已发到 slot1（用户想回话会以指令形式送达）」并立即返回：无按钮、不等待，退出码 0 已发 · 1 输入不合格 · 3 通道故障 · 4 需要人介入。提问挂着的时候也能发。ntfy app 里**一个 topic 只有一个输入框**、不是每张卡一个：有提问挂着时你发的任何内容都算那个提问的回复；没有提问在等时才注入 agent 的会话（§2）。通知与提问共用同一份 ntfy.sh 配额（§8），所以 SKILL.md 要求 agent 别拿它碎碎念。

**日常维护。** 槽位租出去之后不会自动收回（`agent-ntfy slots` 看谁占着哪个——项目路径、从何时起、哪个窗格——`agent-ntfy release <slot>` 释放）。五个全被占满后 agent 会来问你释放哪个、还是 `add-slot` 新建——这是常态，不是故障。

## 6. 手机不弹通知怎么办（Android / MIUI 排查清单）

要认识的失败形态：**消息到了服务端（HTTP 200）、到了手机（ntfy app 里该 topic 下看得见），就是不弹通知——而且哪里都没有报错。** 在 agent 那侧这和「用户还没回」一模一样；`ask` 会一直等一个你根本不知道存在的提问。§5 第 2 步的确认闸就是为了在第一条正式提问之前抓住它。

小米手机实测：权限没配好时，默认 / 高 / 最高三档优先级一条都不弹；设置修好后三档全弹。调高优先级没用，修设置才有用。逐条过，它们互相独立：

- ntfy 的**通知权限**，且里面每个通知*类别*都打开（MIUI 按类别各有开关）
- ntfy 的**省电策略 / 后台限制**设为*无限制*
- 允许 ntfy **自启动**
- 允许 ntfy **锁屏通知**
- ntfy app 里这个 topic **没被静音**，app 自身的通知开关是开的
- 改完任何一项，跑 `agent-ntfy confirm-sub slotN --again`，等它弹出来再点按钮

其他 Android ROM 有同样的开关、名字不同；本项目只实测过 MIUI。

## 7. 安全须知

- **topic 名就是密码。** 知道它的人能看到每一条提问、每一条回复，装了 herdr 的话还能直接往你的 agent 里打指令。设计上没有第二道锁（通路不过滤内容）。别截图、别发聊天、别进 git。
- topic 是前缀后接 20 位随机小写字母与数字（约 2^103 种可能）。池子存在哪取决于平台（§9 `AGENT_NTFY_STORE`）：**macOS 钥匙串**（按 app 授权）；**Windows** 上是 DPAPI 加密的文件 `~/.agent-ntfy/topics.dpapi`（只有同一台机器上的同一个 Windows 用户能解开）；其余平台是一个明文 `0600` 文件 `~/.agent-ntfy/topics.json`。后两种同一用户账户下的其他进程都能读——比钥匙串宽的边界；要么接受，要么自建 ntfy。租约文件（`~/.agent-ntfy/leases.json`：槽位号、持有者身份、时间戳、窗格 id）、项目级状态文件与 daemon 日志里从不出现 topic 名与消息正文。
- **内容明文经过 ntfy.sh。** 提问会描述你的项目；别往里放密钥。
- 要轮换全部 topic：macOS 上删掉钥匙串条目（账户 `agent-ntfy`、服务 `AGENT_NTFY_TOPICS`，如 `security delete-generic-password -a agent-ntfy -s AGENT_NTFY_TOPICS`）；其他平台删掉 `topics.json` / `topics.dpapi`。然后重启 daemon：会生成新池子、作废旧租约、每个槽位都要重新确认。
- **别手建钥匙串条目。** 程序按账户 `agent-ntfy` *加* 服务 `AGENT_NTFY_TOPICS` 查找；账户名不同的条目它看不见，会静默另建一个池子，而你以为自己建的那条在用。

## 8. 已知边界

- Linux 与 Windows 只有单元测试覆盖，没有在真实环境里跑过端到端（§2.1）。欢迎带着真机报告提 PR。
- ntfy.sh 免费额度约 **每来源 IP 每天 250 条**，提问、通知、「已回复」更新、回执、确认消息共用。日常够用；不够就自建 ntfy 并设 `AGENT_NTFY_URL`。
- ntfy.sh 缓存消息 12 小时；手机离线超过这个时长就收不到。`ask` 默认超时也是 12 小时，同一个理由。
- 一张卡片一个按钮，2–5 个选项，正文 ≤ 3584 字节，title ≤ 960 字节（`notify`：正文渲染后 ≤ 4096 字节）。超限会带着实际数字被拒绝，不截断。
- daemon 不认代理环境变量（见 §2）。
- 手机 → agent 的注入需要 herdr（§2）。它不判断 agent 忙不忙（你的 CLI 自己排队），但会核实那个窗格还在。

### 8.1 支持哪些 agent CLI

两个方向，两个不同的答案。

- **提问侧（agent → 手机 → agent）**：任何能跑 shell 命令的 agent CLI 都行，机器上有 Python 3.10 或更新即可。没有名单；skill 只依赖 stdin、stdout 和退出码。
- **注入侧（手机 → agent，需要 herdr）**：herdr 能托管的 agent 种类。herdr 0.9.0 的 `herdr agent start --help` 列了 23 种：pi、claude、codex、gemini、cursor、devin、agy、cline、omp、mastracode、opencode、copilot、kimi、kiro、droid、amp、grok、hermes、kilo、qodercli、qwen、maki、muse。daemon 读窗格的 agent 种类、按种类唤醒目标：`claude` 只发 `herdr agent prompt`（文本并进它当前那一轮）；`kimi` 先 `prompt` 再补 `ctrl+s`（不补这一下它只排队、忙着时不读）；其余种类只发 `prompt`，**未实测**。

真机实测过的：**claude**（提问与注入，含断网后回放、daemon 重启后注入）与 **kimi**（注入与唤醒；`ask` 验证到租到槽位那一步）。其它种类按 herdr 文档理论可用，未实际跑过。

## 9. 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `AGENT_NTFY_HOME` | `~/.agent-ntfy` | 状态目录（目录 0700、文件 0600，在有这些权限位的平台上）：`daemon.sock` 或 `daemon.port`（见 `AGENT_NTFY_IPC`）、`daemon.pid`、`daemon.log`、`leases.json`，以及用到时的 topic 池文件（`topics.json` / `topics.dpapi`，见 `AGENT_NTFY_STORE`）。用 unix socket 传输时路径别太深：socket 路径有一个随系统而异的长度上限，太深 daemon 拒绝启动并提示「无法监听 IPC：…。unix socket 路径有长度上限（系统上限），换一个短一点的 AGENT_NTFY_HOME」 |
| `AGENT_NTFY_LANG` | （系统 locale，否则 `en`） | 一切固定文案的语言（卡片标签、按钮、回执、CLI 输出、`--help`）：`zh` 或 `en`。解析顺序：命令行 `--lang zh\|en`（顶层选项，放在子命令前）> 本变量 > 系统 locale（`LC_ALL` / `LC_MESSAGES` / `LANG` 以 `zh` 开头，或 Windows 的中文区域 ⇒ `zh`）> `en`。别的值直接报错，不静默回退（给了 `--lang` 时以它为准、忽略本变量）。agent 可以用 JSON 里的 `lang` 字段按条覆盖。CLI 自己开 herdr 窗格（`away on` 起 daemon、`confirm-sub` 替你开确认窗格）或 detach 起 daemon 时，会把解析出的语言用 `--lang` 带过去，窗格自己的 shell 不决定文案；只有你手工在窗格里起的 daemon / 命令才继承那个窗格的环境 |
| `AGENT_NTFY_TARGET` | `proj:<项目根>` | 租槽位的身份（`slots` 里显示的租约持有者就是它）。想让几个项目共用一个槽位、或把某个项目单独隔开就设它；同一个值永远复用同一个槽位 |
| `AGENT_NTFY_URL` | `https://ntfy.sh` | 换一个 ntfy 实例，如自建 |
| `AGENT_NTFY_IPC` | macOS / Linux 上 `unix`，Windows 上 `tcp` | CLI 与 daemon 之间的传输。`unix`：unix socket `daemon.sock`，靠文件权限保护。`tcp`：回环 TCP 端口；`daemon.port` 两行——端口与一个随机口令——每个请求的首行都带这个口令（否则本机任何进程都能连上）。残留的端点文件按残骸处理，除非对它发起连接真的连上了（「端口连得上 ⇒ 已有实例在跑」）；要是碰巧被无关进程占住了那个端口，删掉 `daemon.port` 再起 daemon。Windows 上不接受 `unix`；其他值直接报错 |
| `AGENT_NTFY_STORE` | macOS 上 `keychain`，Windows 上 `dpapi`，其余 `file` | topic 池存哪：`keychain`（macOS 的 `security` 命令；其他平台报「找不到 security 命令（钥匙串只在 macOS 上有）；别的平台设 AGENT_NTFY_STORE=file（Linux）或 dpapi（Windows）」）、`file`（`topics.json`，权限 0600）、`dpapi`（`topics.dpapi`，仅 Windows——其他平台报「DPAPI 只在 Windows 上可用（当前平台 …）；别的平台用 AGENT_NTFY_STORE=file 或 keychain」）。池文件解不开（换了用户 / 机器）或内容不是字符串数组时，报错带文件路径；把它移走后重启就是新池子（手机要重新订阅）。三种各挡住什么见 §7 |
| `AGENT_NTFY_KEYCHAIN` | `AGENT_NTFY_TOPICS` | 存 topic 池的钥匙串服务名（macOS、`keychain` 存储时才用） |
| `AGENT_NTFY_TOPIC_PREFIX` | `agent-ntfy` | 新生成 topic 名的前缀（`<前缀>-<20 位随机串>`）；字母、数字、`-`、`_`，最长 40 |
| `HERDR_ENV`、`HERDR_PANE_ID` | herdr 设置 | 自动检测，不用你配：在 herdr 里，`ask`、`notify`、`slots`、`release`（不带参数）、`away on`、`away status` 会把当前窗格记到项目的租约上，手机消息就注入到它（`confirm-sub`、`release <slot>`、`add-slot`、`daemon`、`away off` 不碰它） |

命令行的 `--home <目录>`（放在子命令前面）覆盖 `AGENT_NTFY_HOME`。

### 9.1 远程模式与项目级状态文件

skill 不决定 agent **什么时候**该往手机问——那是你的策略（写在你 agent 的配置 / 规则里）。skill 给这条策略的是一个开关和一个能读的落点：

```
agent-ntfy away on        # 我走了：要拍板的事推到手机
agent-ntfy away off       # 我回来了
agent-ntfy away status    # 人读；加 --json 打印原文
```

`away on` 是一站式的：没有 daemon 应答就起一个（在 herdr 里开新窗格起，否则用 `--detach`）；保证本项目有一个已过闸的槽位、或者池里有空闲的已过闸槽位（都没有时，在 herdr 里就开一个确认窗格、告诉 agent 该让你看哪个窗格，确认结束时窗格会把结果送回 agent 的会话；不在 herdr 里就退 4 并写明要跑的 `confirm-sub` 命令）；这些都成了才在 `<项目根>/.agent-ntfy/` 建目录（项目根 = git 仓根，不在仓里就是当前目录），目录自带 `.gitignore`（内容 `*`，git 看不到它，你仓里的 `.gitignore` 不动），内有 `state.json`：

```json
{"away": true, "slot": "slot2", "confirmed": true, "target": "proj:/path/to/project", "updated": "2026-09-13T21:04:11+08:00"}
```

`slot` / `confirmed` / `target` 由 `ask`、`notify`、`confirm-sub`、`release`、`away` 顺手刷新——但**只在目录已存在的项目里**，没启用过远程模式的项目不会被建目录。`away status` 会向 daemon 要租约、两边不一致时按 daemon 改写文件（并打印「已按 daemon 的租约校正状态文件」）；daemon 没跑就照旧读文件并注明未校对。topic 名永远不写进去。`away` 为 `true` 期间 `ask` / `notify` 只用已过闸的槽位——没有人在键盘旁替新槽位过闸。

`away status --json`（给 agent 读的那个形态）要在 agent 所在的 herdr 窗格里、或它起的子进程里跑：它和 `ask` / `notify` / `slots` 一样会把当前窗格记到租约上，从别处跑会把手机消息指到错的窗格。一条典型的规则是：*`.agent-ntfy/state.json` 里 `away: true` ⇒ 一切要我拍板的事用 `agent-ntfy ask`；后台跑（前台工具调用几分钟就会被杀、卡片作废）；做完 `release`。*

## 10. 已知行为

真机观察到的，都不是 bug。

- **daemon 重启后，手机上还留着旧回执。** 点上一个 daemon 进程发的回执按钮，动作照常执行，但你收到的是一条新的短消息说明结果，旧卡片不会原地更新。手动删掉即可。
- **断网。** 短于约 90 秒 daemon 根本察觉不到（连接自己恢复）。更长则带退避重连，期间你发的消息会回放一次、不重复。断开满 60 秒或连续重连失败 3 次后，正在等的 `ask` 会打印一行「agent-ntfy: 提醒：…」并继续等。
- **以 `-` 开头的回复**（`-v`、`--help`、`- 条目`）原样注入，不会被当成选项解析。
- **停 daemon 时**别从手机发消息。关停窗口里被消费掉的消息只能收到 best-effort 的回执（「daemon 正在停止，你刚才的消息未送达，请稍后再发。」）；连回执都失败的话就丢了（daemon 启动不回放历史）。
- **冷启动不回放。** 没有 daemon 在跑的时候发的消息不会被事后投递；手机上留着，agent 永远看不到。
- **目标 claude 若起在它还没信任的目录**，会停在信任对话框上；注入的文字在那里等着，直到有人回答对话框。
- **同一张卡片点两次**、或先点后打字，产生两条消息。第一条关闭提问；第二条作为指令注入。SKILL.md 已要求 agent 以最后一条为准。
- **极短的 `--timeout`。** 超时短于约 90 秒时，手机上那张卡可能永远不会翻成「⌛ 已超时 · …」（观察到的是：5 秒超时的没翻，90 秒的翻了；原因未查明）。退出码与之后的行为不受影响；默认超时本来就是 12 小时。
- **Android 上有序列表变圆点。** ntfy 的 Android app 把 Markdown 的 `1.` 列表渲染成圆点，所以卡片把选项编号写成 `1\.`（§5）。`notify` 正文里的有序列表同样如此。

## 11. CLI 参考

`AGENT_NTFY_LANG=zh python3 scripts/agent_ntfy.py --help` 与各 `<子命令> --help` 的输出（`slots` 与 `add-slot` 没有选项），主目录显示为 `~`：

```
usage: agent-ntfy [-h] [--lang {zh,en}] [--home HOME]
                  {ask,notify,daemon,slots,release,confirm-sub,add-slot,away} ...

经 ntfy.sh 把需要人拍板的事推到手机，并把裁决带回来

positional arguments:
  {ask,notify,daemon,slots,release,confirm-sub,add-slot,away}
    ask                 阻塞提问，JSON 从 stdin 读
    notify              单向通知（无按钮、不等回复），JSON 从 stdin 读：{"title", "body"}
    daemon              常驻订阅进程
    slots               看槽位池与租约
    release             释放租约
    confirm-sub         可达性确认闸：验该槽位手机收得到通知（在终端跑会显示 topic 名；agent 在 herdr
                        里代跑会自动开一个窗格让用户在那里做）
    add-slot            新建一个槽位
    away                远程交互模式开关：on 一站式（起 daemon、保证有能用的槽位、再在项目根写 .agent-
                        ntfy/state.json 给 agent 读，不含 topic 名）

options:
  -h, --help            show this help message and exit
  --lang {zh,en}        文案语言（zh / en；不给则按 AGENT_NTFY_LANG，再按系统 locale，再缺省 en）
  --home HOME           状态目录（默认 ~/.agent-ntfy）

usage: agent-ntfy ask [-h] [--timeout TIMEOUT]

options:
  -h, --help         show this help message and exit
  --timeout TIMEOUT  等回复的秒数（默认 12 小时）

usage: agent-ntfy notify [-h]

options:
  -h, --help  show this help message and exit

usage: agent-ntfy daemon [-h] [--detach | --status | --stop]

options:
  -h, --help  show this help message and exit
  --detach    脱离会话在后台跑
  --status    看 daemon 状态
  --stop      停掉 daemon

usage: agent-ntfy release [-h] [slot]

positional arguments:
  slot        要释放的槽位；不给就释放当前目标租的那个

options:
  -h, --help  show this help message and exit

usage: agent-ntfy confirm-sub [-h] [--again] [--subscribed] [--show-topic]
                              [--close-pane] [--report-to PANE]
                              [--timeout TIMEOUT]
                              slot

positional arguments:
  slot               要确认的槽位

options:
  -h, --help         show this help message and exit
  --again            已确认过的槽位重新确认（换手机后）
  --subscribed       用户已订阅：不显示 topic，直接发测试通知（非终端也能跑）
  --show-topic       只打印 topic 名就退出，不发测试通知（会进调用方的输出）
  --close-pane       确认成功后问一句要不要关掉当前 herdr 窗格（自动开的窗格带这个）
  --report-to PANE   结束时把结果注入回这个 herdr 窗格里的 agent（自动开的窗格带这个，值是开它的窗格 id）
  --timeout TIMEOUT  等按钮点击的秒数（默认 600）

usage: agent-ntfy away [-h] [--json] {on,off,status}

positional arguments:
  {on,off,status}  on 开 / off 关 / status 看状态

options:
  -h, --help       show this help message and exit
  --json           status 时打印 state.json 原文（给 agent 读）
```

`ask` 的退出码：0 回复在 stdout · 1 输入不合格，什么都没发 · 2 超时 · 3 通道故障（daemon 没跑、连接断开、发布失败；stderr 写明消息发没发出去）· 4 需要人介入（槽位未确认、槽位全被租用、该目标已有提问在等）· 130 Ctrl-C。`notify`：0 已发 · 1 输入不合格 · 3 通道故障 · 4 需要人介入（没有 2：它不等回复）。`confirm-sub`：0 已确认（或已在 herdr 窗格里开始确认）· 1 槽位名不对 · 2 超时内没按回车或没点按钮 · 3 通道故障 · 4 不在终端里且开不了 herdr 窗格（且没给 `--subscribed`）、回车前 stdin 已到头，或槽位正忙 · 130 Ctrl-C。各种情况的 stderr 原文见 [references/failures.md](references/failures.md)（英文）。

不设 `AGENT_NTFY_LANG` 时同样的 help 与文案是英文。daemon 日志始终是中文，与语言设置无关。

## 12. 版本与升级

版本就是 git tag `vX.Y.Z`；改了什么见 [CHANGELOG.md](../../CHANGELOG.md)。`skills` CLI 与 skills.sh 都不读版本号——装到本机的是仓库内容的一份快照，`npx skills update` 刷新它（全局安装加 `-g`，当前项目加 `-p`）。想停在某个版本，安装时把 tag 当 git ref 带上，按 `skills` CLI 的文档，之后 `update` 会停在那个 ref 上：

```bash
npx skills add 'yezhoujie/agent-ntfy-skill#v0.1.0' --skill agent-ntfy
```

**给一台已经跑着 daemon 的机器升级**——按这个顺序：

1. **用你现在手上的 CLI** 停掉正在跑的 daemon：`agent-ntfy daemon --stop`。文件已经换成新版的话，改用 `kill -TERM <pid>`（pid 在 `~/.agent-ntfy/daemon.pid` 里）。原因：从 0.1.0 起 `--stop` 是经 socket 向 daemon 发命令；旧版 daemon 不认这条命令，新版 CLI 会报「没有确认停止」并退 1。
2. 换文件：`npx skills update`（或再跑一遍安装命令、或拷目录）。
3. 起新 daemon：`agent-ntfy daemon --detach`，然后 `agent-ntfy daemon --status` 的行尾应有「传输：unix」（Windows 上是 `tcp`）。不管怎样都必须重启：旧版 daemon 会忽略新版 CLI 发的字段。
4. 跑 `agent-ntfy slots`。0.1.0 之前租下的槽位，持有者显示的是 `wG:p1` 这样的窗格 id 而不是 `proj:<路径>`；用 `agent-ntfy release <slot>` 释放它们（不带参数的 `release` 只找得到当前项目的租约）。
5. 在 agent 工作的那个 herdr 窗格里跑一次 `agent-ntfy slots`（或 `ask` / `notify` / `away status`），让项目的租约记下这个窗格；手机消息就注入到它。

其余不需要迁移：状态目录布局与 `state.json` 没变，缺省值（`AGENT_NTFY_IPC`、`AGENT_NTFY_STORE`）在 macOS 上就是原来的行为。

## 13. 集成方式：让 skill 在整个会话周期里生效

skill 只提供三条命令——`ask`、`notify`、`away`——**有意不规定什么时候用**（SKILL.md「When to use」）。不加约束的
agent 只在碰巧想起这个 skill 时才用它，你离席时靠不住。触发策略要写进 agent 的**常驻指令**（它每个会话都会
加载的那份文件），而且要覆盖四个时刻：

1. **会话开始 / 上下文被清空后**：读 `<项目根>/.agent-ntfy/state.json`（`away status --json`）；`away: true`
   就表示人不在、从现在起每个决定都走手机。
2. **人要走了**（「我走了，有事发手机」）：趁他还在键盘旁跑 `away on`，把输出原样转告——手机上那两下
   （订阅、点按钮）没人能代做。
3. **离席期间**：每一次提问、确认、授权都变成一张 `ask`（放后台跑、同一时刻只挂一张、按退出码办）；手机来的
   消息带 `[agent-ntfy remote] ` 前缀注入会话；`notify` 只用于回答手机上问的问题和**不需要拍板的重大事项**
   ——任务完成、出错、任务无法继续——不用来报进展（配额，§8）。
4. **人回来了**：先 `release`，再 `away off`；daemon 留着。

skill 自带一份照这四条写好的规则：[`examples/remote-mode-rule.zh-CN.md`](examples/remote-mode-rule.zh-CN.md)（中文）、
[`examples/remote-mode-rule.md`](examples/remote-mode-rule.md)（英文），也写了多个 agent 会话组队时怎么办
（只让对接用户的那个会话持有远程模式）。Claude Code 的 `~/.claude/rules/` 会注入每个会话：

```bash
cp ~/.claude/skills/agent-ntfy/examples/remote-mode-rule.zh-CN.md ~/.claude/rules/agent-ntfy-remote-mode.md
```

其他 agent 放到它加载常驻指令的位置。按自己的习惯改开头的 `<skill dir>` 路径和触发用语（「我走了」「我回来了」），
其余是产品行为，照写即可。
