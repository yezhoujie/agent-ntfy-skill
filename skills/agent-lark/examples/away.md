---
description: 我要走开了 —— 开启飞书远程模式，之后要拍板的事推到手机
---
用户要离开键盘了。执行 `herdr-lark away on`（在当前项目目录下），把 stdout 原样转告用户。

它会一条命令备齐整条通路：检查凭据 → 没跑就后台起 daemon → 这个项目没飞书群就建一个（已有则复用）→ 打开开关。
任一步失败都不会把开关留在半开状态，按退出码处理：4 = 需要用户本人动手（通常是没 setup 过），把 stderr 转告他。

开启后：一切本来要问用户、要他确认授权的时刻，改用 `herdr-lark ask` 推到飞书，不要在终端干等。
详细规矩见 ~/.claude/rules/herdr-lark-remote-mode.md。

参数（可选）：$ARGUMENTS —— 若包含分钟数（如 `30`），用 `herdr-lark away on --idle 30`，让跑满该时长的长任务结束时也推一张卡。
