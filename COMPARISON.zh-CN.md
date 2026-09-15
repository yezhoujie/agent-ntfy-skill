# 选 agent-ntfy 还是 agent-lark？

两个 skill 做的是同一件事——agent 把自己拿不定的事推到你的手机、阻塞、把你的裁决原样收回 stdout；没有提问挂着时你发的任何消息都会作为指令送到 agent——命令面也一样（`ask` 吃同一份八字段 JSON、`notify`、`away on|off|status`、同一套退出码 0–4）。差别在通道，下面的一切都由此而来。英文版：[COMPARISON.md](COMPARISON.md)。

出处，方便你核对：agent-ntfy 的 [README](skills/agent-ntfy/README.zh-CN.md)（§2、§7、§8、§9）与 [SKILL.md](skills/agent-ntfy/SKILL.md)；agent-lark 的 [README](skills/agent-lark/README.zh-CN.md)（§2、§9、§10）与 [SKILL.md](skills/agent-lark/SKILL.md)。

## 一分钟选定

- 想五分钟内跑起来、什么都不用注册：**agent-ntfy**。装 ntfy app、订阅一个 topic、点一下测试通知，完事。
- 你本来就在飞书里，或者想让照片、文件、语音也能送到 agent、想要不止一个按钮、想要一条自己的通道而不是公共服务：**agent-lark**。扫一次二维码就建好应用。
- 用 iPhone：agent-lark。ntfy 的 iOS app 收得到通知但没有回复框（agent-ntfy README §2.2 给了网页版的变通）。

## 逐项对照

| | agent-ntfy | agent-lark |
|---|---|---|
| **通道** | [ntfy.sh](https://ntfy.sh) 公共推送服务（或自建 ntfy 实例，`AGENT_NTFY_URL`）；每个项目从池里租一个随机 topic | 你自己的飞书自建应用；每个项目一个飞书群 |
| **手机上要装什么** | ntfy app；不需要任何账号 | 飞书 / Lark，用你自己的账号登录——个人账号就够，不需要管理员 |
| **机器上的运行时** | Python ≥ 3.10，只用标准库 | Node.js ≥ 22，单个自包含文件 |
| **首次配置** | 手机订阅终端里显示的 topic，在测试通知上点按钮（`confirm-sub`，每个 topic 一次） | 终端里跑 `setup`：菜单二选一——用飞书扫二维码，或输入已有应用的 App ID 与 App Secret（不回显）；然后跟 agent 说开启远程模式——它跑 `away on` 建本项目的群并把你拉进去 |
| **平台** | macOS 真机端到端；Linux / Windows 只有 CI 单测 | 一样：macOS 真机端到端（在 herdr 内）；Linux / Windows 只有 CI 单测 |
| **herdr** | 可选：手机 → agent 注入、`away on` / `confirm-sub` 开窗格需要它 | 可选：手机 → agent 注入、🔔「等你输入」卡需要它；其余都不需要 |
| **你怎么回答** | 一个按钮「采纳推荐」，或在 topic 里打字 | 每个选项一个按钮（2–5 个）；不可逆选项红色 + 二次确认；`select: "multi"` 是勾选框 + 提交按钮；或在群里打字 |
| **agent 收到什么** | 点按钮 ⇒ 推荐项的 label；打字 ⇒ 原文 | 点按钮 ⇒ 那个选项的 label；勾选 ⇒ 所选 label 用「、」拼起来；打字 ⇒ 原文 |
| **agent → 手机，提问之外** | `notify`（标题 + 正文） | `notify`（标题 + 正文）、`send-file`（图片或文件）、提问加 `--urgent`（飞书应用内加急） |
| **手机 → agent，文字之外** | 只有文字 | 照片和文件（下载后把路径交给 agent）、语音（转写；需要付费版飞书租户）、引用回复某张卡时带上那张卡的标题 |
| **手机上的送达反馈** | 只在没送到时来一张回执卡 | 送进终端的每条消息贴一个 `Get` 表情；没送到时来一张回执卡 |
| **agent 卡住提醒** | 无 | herdr 报会话卡在提示上时推 🔔 卡（远程模式开着，每分钟最多一次） |
| **消息大小** | 卡片正文 ≤ 3584 字节、标题 ≤ 960 字节（ntfy 的上限） | 标题 ≤ 200 字、各文本字段 ≤ 4000 字、`notify` 正文 ≤ 8000 字 |
| **留存** | ntfy.sh 缓存 12 小时；手机离线更久就收不到（`ask` 默认超时 12 小时正因此） | 卡片就是群里的一条消息，通道侧不过期（`ask` 默认超时同样 12 小时） |
| **配额** | ntfy.sh 每 IP 每天约 250 条，提问、更新、回执共用 | 没有按天的消息配额；受飞书 API 频率限制（正常使用没量过） |
| **要保密的是什么** | topic 名——知道它的人能看到一切、能给 agent 下指令 | app secret（存 OS 钥匙串，不进 argv、不进任何输出）——以及群成员：群里的任何人都能驱动 agent |
| **内容经过哪里** | 明文经 ntfy.sh | 经飞书服务器；群描述里带着项目的绝对路径 |
| **有提问挂着时停 daemon** | 直接停，等着的 `ask` 退 3 | 拒绝；`--force` 才停并取消 |
| **任务收尾** | `release`：槽位还回池里（`away off` 也会做） | 先问用户群留不留：`unbind`（群留在飞书里，下次会问要不要改名复用）或 `unbind --dissolve`（解散并忘掉）；已不存在的群 daemon 每天自动忘掉；`away off` 只关开关 |
| **固定文案的语言** | JSON 里的 `lang`；否则 `--lang` / `AGENT_NTFY_LANG` / 系统 locale | JSON 里的 `lang`（缺省 `en`）；CLI 本身只有英文 |

## 两个都装了

它们互不知道对方，也都不决定一件事该走哪边。这是你给 agent 的常驻规则的事：每台机器（或每个项目）只让一条远程模式规则生效，由它点名调用哪个 CLI——[agent-ntfy 的规则](skills/agent-ntfy/examples/remote-mode-rule.zh-CN.md)或 [agent-lark 的规则](skills/agent-lark/examples/remote-mode-rule.zh-CN.md)（各自旁边都有另一种语言的版本）。两个 daemon 并存没有问题，它们不共享任何东西。
