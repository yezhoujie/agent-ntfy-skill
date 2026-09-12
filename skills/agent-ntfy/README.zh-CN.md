# agent-ntfy

[English](README.md) · 中文

让任意 AI coding CLI 把它自己拿不定的事经 [ntfy](https://ntfy.sh) 推到你的手机，再把你的裁决——或任何一句指令——直接送回 agent 的会话。
不需要服务器、不需要固定 IP、不需要付费服务，除 Python 3 外零依赖。

本文写给装它的人。agent 读的是 [SKILL.md](SKILL.md) 与 `references/`，你不必向它解释这个工具。

## 目录

1. 工作原理
2. 前提
3. 安装
4. 你要动手的只有两件事
5. 第一条提问，从头到尾
6. 手机不弹通知怎么办（Android / MIUI 排查清单）
7. 安全须知
8. 已知边界
9. 环境变量
10. 已知行为
11. CLI 参考

## 1. 工作原理

```
agent ──ask（stdin 里的 JSON）──▶ agent-ntfy ──unix socket──▶ daemon ──HTTPS──▶ ntfy.sh ──▶ 你的手机
      ◀── 回复经 stdout 返回 ───            ◀───────────────         ◀── SSE ────         ◀── 点按钮 / 打字
                                                                       │
                                                             没有提问在等？
                                                                       ▼
                                                        注入到 agent 所在的 herdr 窗格
```

- agent 交来一段含 8 个必填字段的 JSON；CLI 把它渲染成固定版式的卡片、**只带一个按钮**（「采纳推荐」）并推送。agent 阻塞等你点按钮或打字，你的回复原样返回给它。
- 常驻 **daemon** 是唯一的 ntfy 订阅者。没有提问在等的时候你发的任何内容，会作为一句普通指令注入 agent 的会话（这一步需要 [herdr](https://herdr.dev)；没装的话你会在手机上收到一条「未送达」回执）。
- 每个 agent（一个 herdr 窗格，或你配置的目标标识）租用一个**槽位** = 池子里的一个随机 ntfy topic，池子存在 macOS 钥匙串里。回复按 topic 路由。
- 通路只搬运文字：不解释、不代答、不去重。

## 2. 前提

- macOS（topic 池存钥匙串；暂无 Linux 后端）
- Python 3.10 或更新——只用标准库，不用 `pip install` 任何东西
- 手机上装 [ntfy app](https://ntfy.sh)；不需要 ntfy.sh 账号。实测环境是 Android（MIUI）；ntfy 也有 iOS 版，本项目未测
- 可选：[herdr](https://herdr.dev)，要「手机 → agent」下指令才需要；没有它，「agent → 手机」的提问照常可用
- 能出站 HTTPS 访问 `ntfy.sh`（或你自建的实例）。daemon **不读** `http_proxy` / `https_proxy` 与系统代理；对进程透明的 TUN 型 VPN 可以

## 3. 安装

```bash
npx skills add yezhoujie/agent-ntfy-skill --skill agent-ntfy -g
```

它把 `skills/agent-ntfy/` 拷进你 agent 的全局 skills 目录。任何能把这个目录放到 agent 加载 skill 位置的办法都行（`git clone` 后拷目录也一样）。

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
agent-ntfy daemon --detach        # 任何环境：daemon：已在后台启动，pid 12345（日志 ~/.agent-ntfy/daemon.log）
agent-ntfy daemon                 # 在 herdr 里：改在一个空闲窗格里前台跑，看得见
agent-ntfy daemon --status        # daemon：pid 12345  订阅：已连上  等待中的提问：0  确认中：0  槽位：5
```

这里的路径用 `~` 缩写；CLI 打印的是展开后的绝对路径。要看到中文文案，先在 shell 里 `export AGENT_NTFY_LANG=zh` 再做第 1 步：daemon 的语言以启动它的那个 shell 为准，之后不再改。`--detach` 若报「5 秒内还没就绪，仍在启动（钥匙串弹窗？）」，看一眼屏幕有没有钥匙串授权对话框（池子是经 `security` 命令读的），答完再跑 `--status`。绝不要把 daemon 当作 agent 自己 shell 的后台任务起：agent 一退出它就没了。

**第 2 步——确认手机收得到槽位 1 的通知。** 在你自己的终端跑（别经 agent：它会打印 topic 名，那就是密码）：

```
$ agent-ntfy confirm-sub slot1
slot1 的 topic：agent-ntfy-xxxxxxxxxxxxxxxxxxxx
订阅地址：https://ntfy.sh/agent-ntfy-xxxxxxxxxxxxxxxxxxxx
在手机 ntfy app 里订阅上面这个 topic；订阅好后按回车，我会发一条带按钮的测试通知——看到它弹出来、点按钮，确认就完成了。
订阅好了就按回车…
agent-ntfy: 测试通知已发出，请在手机通知栏点「我收到了」（600 秒内）…
✅ slot1 已确认：手机收得到通知，之后 agent 可以用它提问了
```

只有点按钮算数，而且要点**弹出来的那条通知**——在 app 里点证明不了通知会弹（见 §6）。10 分钟内没弹出来命令退出 2；把手机设置修好再跑一次。

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

手机上出现卡片。点 **采纳推荐**，终端打印 `蛋糕`；改在 app 的输入框里打「当然是派」，终端就打印 `当然是派`。手机上那张卡片会变成「✅ 已回复 · …」，你的回复在上面、原提问保留在下面。

然后跑 `agent-ntfy release`（不带参数就释放本 shell 租的槽位）。这次测试把 slot1 租给了你的终端会话；agent 是另一个身份，不释放的话它会拿到 slot2——未确认——再把你拉回第 2 步。

**第 4 步——交给 agent。** 它自己读 SKILL.md。碰到还没确认过的槽位时，它会以退出码 4 失败并请你去跑 `confirm-sub slotN`（第 2 步）——这是设计，不是 bug：topic 名不能经过 agent 的输出。

**日常维护。** 槽位租出去之后不会自动收回（`agent-ntfy slots` 看谁占着哪个，`agent-ntfy release <slot>` 释放）。五个全被占满后 agent 会来问你释放哪个、还是 `add-slot` 新建——这是常态，不是故障。

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
- topic 是前缀后接 20 位随机小写字母与数字（约 2^103 种可能），只存 macOS 钥匙串。租约文件（`~/.agent-ntfy/leases.json`）与 daemon 日志里只有槽位号，从不出现 topic 名与消息正文。
- **内容明文经过 ntfy.sh。** 提问会描述你的项目；别往里放密钥。
- 要轮换全部 topic：删掉钥匙串条目（账户 `agent-ntfy`、服务 `AGENT_NTFY_TOPICS`，如 `security delete-generic-password -a agent-ntfy -s AGENT_NTFY_TOPICS`）再重启 daemon：会生成新池子、作废旧租约、每个槽位都要重新确认。
- **别手建钥匙串条目。** 程序按账户 `agent-ntfy` *加* 服务 `AGENT_NTFY_TOPICS` 查找；账户名不同的条目它看不见，会静默另建一个池子，而你以为自己建的那条在用。

## 8. 已知边界

- 只支持 macOS（钥匙串）。存储层是一个独立类，Linux 后端可以补、但还没写。
- ntfy.sh 免费额度约 **每来源 IP 每天 250 条**，提问、「已回复」更新、回执、确认消息共用。日常够用；不够就自建 ntfy 并设 `AGENT_NTFY_URL`。
- ntfy.sh 缓存消息 12 小时；手机离线超过这个时长就收不到。`ask` 默认超时也是 12 小时，同一个理由。
- 一张卡片一个按钮，2–5 个选项，正文 ≤ 3584 字节，title ≤ 960 字节。超限会带着实际数字被拒绝，不截断。
- daemon 不认代理环境变量（见 §2）。
- 手机 → agent 的注入需要 herdr。它不判断 agent 忙不忙（你的 CLI 自己排队），但会核实那个窗格还在。

## 9. 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `AGENT_NTFY_HOME` | `~/.agent-ntfy` | 状态目录：`daemon.sock`、`daemon.pid`、`daemon.log`、`leases.json`（目录 0700，文件 0600）。路径别太深——macOS 上 unix socket 路径上限 104 字节，太深 daemon 拒绝启动并提示「起不了 socket …换一个短一点的 AGENT_NTFY_HOME」 |
| `AGENT_NTFY_LANG` | `en` | 一切固定文案的语言（卡片标签、按钮、回执、CLI 输出、`--help`）：`zh` 或 `en`。别的值直接报错，不静默回退。agent 可以用 JSON 里的 `lang` 字段按条覆盖 |
| `AGENT_NTFY_TARGET` | `host:<主机名>\|sid:<会话 id>` | 不在 herdr 里时，提问方的稳定标识（`slots` 里显示的租约持有者就是它）；同一个值复用同一个槽位。在 herdr 里用窗格 id，此变量被忽略 |
| `AGENT_NTFY_URL` | `https://ntfy.sh` | 换一个 ntfy 实例，如自建 |
| `AGENT_NTFY_KEYCHAIN` | `AGENT_NTFY_TOPICS` | 存 topic 池的钥匙串服务名 |
| `AGENT_NTFY_TOPIC_PREFIX` | `agent-ntfy` | 新生成 topic 名的前缀（`<前缀>-<20 位随机串>`）；字母、数字、`-`、`_`，最长 40 |
| `HERDR_ENV`、`HERDR_PANE_ID` | herdr 设置 | 自动检测，不用你配：在 herdr 里窗格 id 就是租约持有者，也是卡片标题里的 `[tag]` |

命令行的 `--home <目录>`（放在子命令前面）覆盖 `AGENT_NTFY_HOME`。

## 10. 已知行为

真机观察到的，都不是 bug。

- **daemon 重启后，手机上还留着旧回执。** 点上一个 daemon 进程发的回执按钮，动作照常执行，但你收到的是一条新的短消息说明结果，旧卡片不会原地更新。手动删掉即可。
- **断网。** 短于约 90 秒 daemon 根本察觉不到（连接自己恢复）。更长则带退避重连，期间你发的消息会回放一次、不重复。断开满 60 秒或连续重连失败 3 次后，正在等的 `ask` 会打印一行「agent-ntfy: 提醒：…」并继续等。
- **以 `-` 开头的回复**（`-v`、`--help`、`- 条目`）原样注入，不会被当成选项解析。
- **停 daemon 时**别从手机发消息。关停窗口里被消费掉的消息只能收到 best-effort 的回执（「daemon 正在停止，你刚才的消息未送达，请稍后再发。」）；连回执都失败的话就丢了（daemon 启动不回放历史）。
- **冷启动不回放。** 没有 daemon 在跑的时候发的消息不会被事后投递；手机上留着，agent 永远看不到。
- **目标 claude 若起在它还没信任的目录**，会停在信任对话框上；注入的文字在那里等着，直到有人回答对话框。
- **同一张卡片点两次**、或先点后打字，产生两条消息。第一条关闭提问；第二条作为指令注入。SKILL.md 已要求 agent 以最后一条为准。

## 11. CLI 参考

英文界面的 `--help`（`AGENT_NTFY_LANG=zh` 时同样内容是中文）：

```
usage: agent-ntfy [-h] [--home HOME]
                  {ask,daemon,slots,release,confirm-sub,add-slot} ...

Push decisions that need a human to your phone via ntfy.sh, and bring the
verdict back

positional arguments:
  {ask,daemon,slots,release,confirm-sub,add-slot}
    ask                 block and ask: reads the question JSON from stdin
    daemon              the resident subscriber process
    slots               show the slot pool and leases
    release             release a lease
    confirm-sub         reachability check: verify the phone gets
                        notifications for this slot (run it in a terminal by
                        default; it shows the topic name)
    add-slot            add a slot

options:
  -h, --help            show this help message and exit
  --home HOME           state directory (default ~/.agent-ntfy)
```

各子命令的 help，摘要（每个 `<子命令> --help` 打印的是完整 argparse 版式）：

```
usage: agent-ntfy ask [-h] [--timeout TIMEOUT]
  --timeout TIMEOUT  seconds to wait for a reply (default 12 hours)

usage: agent-ntfy daemon [-h] [--detach | --status | --stop]
  --detach    detach from the session and run in the background
  --status    show daemon status
  --stop      stop the daemon

usage: agent-ntfy confirm-sub [-h] [--again] [--subscribed] [--show-topic] [--timeout TIMEOUT] slot
  --again            re-confirm an already confirmed slot (after changing phones)
  --subscribed       user already subscribed: skip showing the topic and send the test notification right away (works outside a terminal)
  --show-topic       only print the topic name and exit, send nothing (it will land in the caller's output)
  --timeout TIMEOUT  seconds to wait for the button tap (default 600)

usage: agent-ntfy release [-h] [slot]     slot to release; omit to release the one leased by the current target
```

`ask` 的退出码：0 回复在 stdout · 1 输入不合格，什么都没发 · 2 超时 · 3 通道故障（daemon 没跑、连接断开、发布失败；stderr 写明消息发没发出去）· 4 需要人介入（槽位未确认、槽位全被租用、该目标已有提问在等）· 130 Ctrl-C。`confirm-sub`：0 已确认 · 1 槽位名不对 · 2 没按时点按钮 · 3 通道故障 · 4 不在终端里（且没给 `--subscribed`）或槽位正忙。各种情况的 stderr 原文见 [references/failures.md](references/failures.md)（英文）。

daemon 日志始终是中文，与语言设置无关。
