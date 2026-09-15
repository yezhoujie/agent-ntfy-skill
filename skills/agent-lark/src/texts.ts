/**
 * Every fixed string the skill prints or renders, in one place.
 *
 * Two audiences, two shapes:
 *   - `zh` / `en`: wording a human reads on the phone (card shell, receipt and
 *     status cards, toasts) plus the interactive `setup` walkthrough. Both
 *     columns must have the same keys (tsc enforces it) and the same
 *     `{placeholder}` set (a test enforces it).
 *   - `msg`: wording an agent reads — CLI stdout / stderr, validation errors,
 *     daemon replies, the HELP text. English only.
 *
 * Placeholders are `{name}`; substitute with `fill()`.
 */
import type { Lang } from './validate.js';

export const zh = {
  // ask card
  doing: '在做',
  background: '背景',
  blocker: '卡点',
  options: '选项',
  recommend: '我的判断',
  question: '你的判断',
  recommended: '← 我推荐',
  optionSep: '：',
  yourReply: '你的回复',
  theQuestion: '（原问题）',
  hint: '想说别的？直接在本群发消息就行，第一条消息就是答复。',
  hintDanger: '红色按钮会二次确认；也可以直接在群里打字。',
  hintMulti: '勾选后点提交；想说别的直接在群里打字，整段就是回复。',
  submit: '提交',
  confirmMultiText: '所选项里有不可逆或高代价的操作。确定提交？',
  pickAtLeastOne: '至少选一项',
  answered: '已回答',
  timedout: '已超时',
  cancelled: '已取消',
  confirmTitle: '确认执行',
  confirmText: '“{label}”是不可逆或高代价的操作。确定选它？',
  // receipt card
  notDelivered: '没能送达',
  notDeliveredBody: '刚才那条消息没能送进终端：{why}',
  receiptNoPane: '这个项目还没有记录到 herdr 窗格，消息没处可送。',
  promptAgentBlocked: '终端里的 agent 正卡在一个需要你本人确认的提示上，收不了新输入。回电脑前处理一下。',
  promptPaneGone: '记录的 herdr 窗格已经不在了。到项目里跑一次 agent-lark away on 或任意 agent-lark 命令，重新记录窗格。',
  promptNoHerdr: '这台机器没有 herdr，或 herdr 没在跑，手机上发的消息没处注入。',
  promptRefused: 'herdr 拒绝了这次注入：{code} {message}',
  // "stuck" status card
  statusBlocked: '等你输入',
  statusPane: '窗格 {pane}',
  // button / form toasts
  toastAnswered: '已回复',
  toastClosed: '这个问题已经结束了，刚才那下当成新指令发过去了',
  toastBadOption: '这个选项对不上，再试一次',
  // setup walkthrough (each line is printed as "zh　/　en")
  setupHaveCreds: '已经有凭据了（来自 {origin}）。想重新授权或补权限，加 --update；想换一个应用，先 agent-lark setup --reset。',
  setupMenu: '怎么接入飞书？\n  1) 扫码新建一个应用（用飞书扫终端里的二维码）\n  2) 复用一个已有的应用（输入 App ID 与 App Secret）',
  setupMenuPrompt: '选 [1/2]：',
  setupMenuBad: '只能输 1 或 2。',
  setupAppIdPrompt: 'App ID（cli_ 开头）：',
  setupAppIdBad: 'App ID 形态不对：应是 cli_ 加字母数字（在开发者后台「凭证与基础信息」里看）。',
  setupSecretPrompt: 'App Secret（输入不回显）：',
  setupProbing: '正在连一次飞书确认这对凭据可用……',
  setupProbeOk: '✅ 凭据可用，应用名「{app}」',
  setupUnnamedApp: '(未命名)',
  setupProbeFailed: '这对凭据连不上飞书：{error}',
  setupSaved: '凭据已保存到：{where}',
  setupSavedQr: '✅ 应用已绑定，凭据保存到：{where}（明文不会出现在任何输出里）。',
  setupNext: '下一步：回到 agent 会话，说「开启远程交互模式」或输入 /agent-lark on——daemon 与群绑定由 agent 从它自己的窗格完成，不用你手动跑。',
  setupReuseGaveUp: '连续 {n} 次没通过，先到开发者后台核对 App ID / App Secret，再跑一次 agent-lark setup --reuse。',
  setupManualScopes: '这个应用要在开发者后台手动开通（应用 → 权限管理 → 开通权限）：',
  setupManualEvents: '事件订阅：im.message.receive_v1（订阅方式选「使用长连接接收事件」）· 回调：card.action.trigger（同样选长连接）',
  setupManualPublish: '开通后发布一个版本，权限才生效。',
  closePanePrompt: '关掉这个窗格？[Y/n] ',
  paneKept: '窗格留着。',
  paneCloseFailed: '关窗格失败：{error}',
  setupRequesting: '正在向飞书申请扫码注册……',
  setupScan: '用飞书扫上面的二维码（扫不到就打开这个链接）：',
  setupScopes: '确认页会列出要授权的权限：',
  setupEvents: '事件 im.message.receive_v1 · 回调 card.action.trigger',
  setupExpiry: '⏳ 二维码 {minutes} 分钟内有效（{time} 过期），过期就重跑 setup。',
  setupWaiting: '  还在等你扫……剩 {seconds} 秒',
  setupStatus: '  状态：{status}',
  setupExpired: '二维码过期了，没等到扫码。重跑一次：agent-lark setup（原始错误：{error}）',
  setupRegisterFailed: '扫码注册失败：{error}',
  setupRetry: '网络抖动（{error}），重新申请一张二维码（第 {n}/{max} 次）……',
  appDesc: '把终端里 agent 的提问推到手机，答复注入回终端',
} as const;

export const en: Record<keyof typeof zh, string> = {
  doing: 'Doing',
  background: 'Background',
  blocker: 'Blocker',
  options: 'Options',
  recommend: 'My recommendation',
  question: 'Your call',
  recommended: '← recommended',
  optionSep: ' — ',
  yourReply: 'Your reply',
  theQuestion: '(the question as asked)',
  hint: 'Want to say something else? Just send a message in this group — the first one is the answer.',
  hintDanger: 'Red buttons ask for confirmation; you can also just type here.',
  hintMulti: 'Tick what applies, then Submit; to say something else just type in the group — the whole message is the reply.',
  submit: 'Submit',
  confirmMultiText: 'The selection includes an irreversible or high-cost option. Submit anyway?',
  pickAtLeastOne: 'Pick at least one',
  answered: 'Answered',
  timedout: 'Timed out',
  cancelled: 'Cancelled',
  confirmTitle: 'Confirm',
  confirmText: '"{label}" is irreversible or high-cost. Go ahead?',
  notDelivered: 'Not delivered',
  notDeliveredBody: 'That message never reached the terminal: {why}',
  receiptNoPane: 'No herdr pane is recorded for this project, so there is nowhere to deliver the message.',
  promptAgentBlocked:
    'The agent in the terminal is stuck on a prompt only you can answer and cannot take new input. Deal with it when you are back at the computer.',
  promptPaneGone:
    'The recorded herdr pane is gone. Run agent-lark away on (or any agent-lark command) inside the project to record the pane again.',
  promptNoHerdr: 'This machine has no herdr, or herdr is not running; messages from the phone have nowhere to go.',
  promptRefused: 'herdr refused the injection: {code} {message}',
  statusBlocked: 'waiting for you',
  statusPane: 'pane {pane}',
  toastAnswered: 'Replied',
  toastClosed: 'This question is already closed; that tap was forwarded as a new instruction',
  toastBadOption: 'That option does not match, try again',
  setupHaveCreds:
    'Credentials already exist (from {origin}). Add --update to re-authorize or add scopes; run agent-lark setup --reset first to switch apps.',
  setupMenu: 'How do you want to connect to Feishu?\n  1) Create a new app by QR code (scan it with Feishu)\n  2) Reuse an app you already have (enter its App ID and App Secret)',
  setupMenuPrompt: 'Choose [1/2]: ',
  setupMenuBad: 'Type 1 or 2.',
  setupAppIdPrompt: 'App ID (starts with cli_): ',
  setupAppIdBad: 'That is not an App ID: expected cli_ followed by letters and digits (Developer console → Credentials & Basic Info).',
  setupSecretPrompt: 'App Secret (not echoed): ',
  setupProbing: 'Checking these credentials against Feishu once…',
  setupProbeOk: '✅ Credentials work; app name "{app}"',
  setupUnnamedApp: '(unnamed)',
  setupProbeFailed: 'These credentials cannot reach Feishu: {error}',
  setupSaved: 'Credentials saved to: {where}',
  setupSavedQr: '✅ App linked; credentials saved to {where} (the secret never appears in any output).',
  setupNext: 'Next: back in your agent session, say "turn remote mode on" or type /agent-lark on — the agent starts the daemon and binds the group from its own pane; nothing to run by hand.',
  setupReuseGaveUp: '{n} attempts failed; check the App ID / App Secret in the developer console, then run agent-lark setup --reuse again.',
  setupManualScopes: 'Enable these scopes for the app by hand in the developer console (app → Permissions & Scopes):',
  setupManualEvents: 'Event subscription: im.message.receive_v1 (delivery: long connection) · callback: card.action.trigger (long connection as well)',
  setupManualPublish: 'Publish a version afterwards; scopes take effect only then.',
  closePanePrompt: 'Close this pane? [Y/n] ',
  paneKept: 'Pane kept.',
  paneCloseFailed: 'closing the pane failed: {error}',
  setupRequesting: 'Asking Feishu for a QR-code registration…',
  setupScan: 'Scan the QR code above with Feishu (or open this link):',
  setupScopes: 'The confirmation page lists the scopes being requested:',
  setupEvents: 'event im.message.receive_v1 · callback card.action.trigger',
  setupExpiry: '⏳ The QR code is valid for {minutes} minutes (expires {time}); rerun setup if it expires.',
  setupWaiting: '  still waiting for the scan… {seconds} s left',
  setupStatus: '  status: {status}',
  setupExpired: 'The QR code expired before it was scanned. Run again: agent-lark setup (original error: {error})',
  setupRegisterFailed: 'QR-code registration failed: {error}',
  setupRetry: 'Network hiccup ({error}); asking for a fresh QR code (attempt {n}/{max})…',
  appDesc: 'Agent questions pushed to your phone, answers back to the terminal',
};

/** Agent-facing wording: CLI output, validation, daemon replies. English only. */
export const msg = {
  help: `agent-lark — reach the agent session running in your terminal from Feishu/Lark

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
  unbind [--dissolve]                Let the live group go (it stays in Feishu; the next away on offers it back);
                                     --dissolve dissolves it in Feishu and forgets it (exit 4 if Feishu refuses: dissolve it by hand)
  bind [--chat <id>] [--name <task>] [--reuse <chat_id> | --new]
                                     Bind without switching remote mode on; --chat names a group outright
  ask [--timeout <seconds>] [--urgent]
                                     Read JSON from stdin, push a question card, block until answered (--urgent flags the owner in-app)
  notify                             Read JSON from stdin, push a titled notification card (important things only)
  send-file <path> [--caption <t>]   Send an image or file to the project group
  status                             Daemon and binding overview

Global: --home <dir>  state directory (same as AGENT_LARK_HOME; default ~/.agent-lark)

Exit codes: 0 ok · 1 bad input · 2 timed out, nobody answered · 3 channel failure · 4 a human must act
`,
  prefix: 'agent-lark: ',
  homeNeedsDir: '--home needs a directory',
  unknownOption: 'unknown option {option}. See agent-lark --help.',
  offline: 'offline: refusing to contact Feishu (AGENT_LARK_OFFLINE=1 is set)',
  unknownCommand: 'Unknown command "{cmd}". See agent-lark --help.',
  needStdin: 'This command reads one JSON object from stdin. Feed it with a heredoc.',
  badJson: 'stdin is not valid JSON: {error}',
  askProblems: 'This question card has {n} problem(s); nothing was sent:',
  notifyProblems: 'This notification has {n} problem(s); nothing was sent:',
  timeoutArg: '--timeout must be a positive integer (seconds)',
  sendFileUsage: 'Usage: agent-lark send-file <path> [--caption <text>]',
  awayUsage: 'Usage: agent-lark away on [--name <task>] [--reuse <chat_id> | --new] | off | status [--json]',
  renameUsage: 'Usage: agent-lark rename "<task name>"',
  taskNameTooLong: 'task name: over {max} characters (code points), got {n}',
  setupHandoffStarted:
    'The interactive setup is running in herdr pane {pane}: the user enters the App ID and App Secret there (they never pass through this session). When it ends, one line prefixed "[agent-lark] setup:" arrives here (that pane has focus now).',
  setupHandoffFailed: 'could not open a herdr pane for the interactive setup ({why}). Ask the user to run it in their own terminal:\n  {command}',
  setupReuseNeedsTerminal:
    'setup --reuse asks for the App ID and App Secret interactively, and there is no terminal here (and no herdr to open one). Ask the user to run it in their own terminal:\n  {command}',
  setupReportOk: '[agent-lark] setup: credentials stored for {appId} ({app}); the scopes must be enabled in the developer console before use',
  setupReportFailed: '[agent-lark] setup: failed: {why}',
  setupReportInterrupted: '[agent-lark] setup: interrupted before any credentials were stored',
  setupReportExists: '[agent-lark] setup: credentials already stored ({origin}); nothing changed. To switch apps run agent-lark setup --reset --reuse',
  setupReportNotDelivered: 'agent-lark: the result could not be reported to pane {pane} ({why})',
  awayOffLocal: 'daemon is not running; local state cleared',
  // daemon command
  daemonNotRunning: 'daemon: not running',
  daemonNoAnswer: 'daemon: no answer ({message})',
  daemonWeird: 'daemon: unrecognized reply',
  daemonStatusLine:
    'daemon: pid {pid}  connected {connected}  connection {connection}  pending questions {pending}  bound projects {bindings}  started {startedAt}',
  daemonLastError: '  last error: {error}',
  daemonMediaLine: 'media: ttl {ttl} days, {mb} MB in {files} files (as of last sweep {at})',
  daemonMediaLineOff: 'media: no automatic cleanup (AGENT_LARK_MEDIA_TTL_DAYS=0), {mb} MB in {files} files (as of last sweep {at})',
  mediaTtlInvalid: 'agent-lark: warning: AGENT_LARK_MEDIA_TTL_DAYS={value} is not a whole number of days; using {fallback}',
  daemonStopStuck: 'daemon: still answering 10 s after the stop request; see the log: {log}',
  daemonWasNotRunning: 'daemon: was not running',
  daemonStopRefused:
    '{n} question(s) still pending on the phone. Stopping the daemon now turns those cards into "⚠️ Cancelled" — a dead card for the human.\nWait for the answer, or do it anyway: agent-lark daemon --stop --force',
  daemonStopped: 'daemon: stopped',
  daemonAlready: 'daemon is already running',
  daemonStarted: 'daemon: started in the background, pid {pid} (log {log})',
  daemonNoReply: 'daemon started but did not answer within 10 s; see the log: {log}',
  daemonNoCreds: 'no Feishu app credentials found. Run agent-lark setup first (or set AGENT_LARK_APP_ID / AGENT_LARK_APP_SECRET)',
  daemonReady: 'agent-lark daemon: pid {pid}, listening at {sock}, connecting to Feishu in the background',
  notConnected: 'not connected to Feishu ({error}); the daemon keeps retrying, try again shortly',
  reconnecting: 'the connection to Feishu dropped, reconnecting',
  connecting: 'still connecting',
  // bind / unbind / rename
  bindCreated: '✅ Created Feishu group "{name}" and bound it to {root}\n   Open Feishu to see the group; questions from this project will land there.',
  bindExisting: '✅ Bound to existing group {chatId} ({root})',
  bindKept: '✅ Already bound to Feishu group "{name}" ({root})',
  bindReused: '✅ Took back Feishu group "{name}" for {root}',
  bindCandidates: 'this project has no live group, but {n} earlier group(s) could be taken back (renamed) instead of creating another:',
  bindCandidateLine: '{name}  released {time}  {chatId}',
  bindCandidateHint: 'ask the user which to reuse (rename) or create new; rerun with --reuse <chatId> or --new',
  bindCandidateUnnamed: '(unnamed)',
  bindCandidateNever: '-',
  bindModeConflict: '--reuse and --new cannot be combined',
  bindReuseUnknown: '--reuse {chatId}: not one of the groups this project could take back',
  bindChatTaken: 'group {chatId} is the live group of another project ({root}); unbind it there first',
  bindScanFailed: 'could not look through the Feishu groups for earlier ones of this project ({error}); only local records were considered',
  bindRenameFailed: 'bound, but {error}',
  bindingsTwoActive: 'bindings: {root} already has one active group ({chatId})',
  bindingsFileBad: 'cannot read the bindings file {path}: {error}\nFix or move it; it is left untouched.',
  bindModeIgnored: 'this project already has a live group; --reuse / --new were ignored (unbind first to pick another group)',
  bindUpdateSkipped: 'not connected to Feishu; the group\'s name and description were left as they are',
  unbound: 'Unbound. The Feishu group "{name}" stays in Feishu; the next away on in this directory offers to rename and reuse it.',
  dissolved: 'Dissolved Feishu group "{name}"; the local record is removed.',
  renameNotBound: 'this project has no live group; run agent-lark away on first',
  renameFailed: 'renaming the group failed: Feishu error {code} {msg}',
  renameThrew: 'renaming the group failed: {error}',
  renamePermissionHint:
    'The bot may only rename a group it owns, or one whose settings let every member edit group info (232002 / 232016), and must be a member of it (232011).',
  renamed: 'Renamed the Feishu group to "{name}"',
  // notify / send-file
  notifySent: 'Notification sent (a reply from the phone is injected into this pane as an instruction)',
  fileSent: 'Sent to the project group',
  // away
  awayNeverUsed: 'This project has never used agent-lark (no .agent-lark/state.json)',
  awayStatusLine: 'remote mode: {away}  group: {chat}',
  awayNotConnected: 'daemon is up but not connected to Feishu: {error}',
  awayOutsideHerdr: 'Not inside herdr: messages sent from the phone are not injected anywhere, and there is no stuck-on-a-prompt alert.',
  on: 'on',
  off: 'off',
  awayUnbound: 'not bound',
  awayNoCreds: 'No Feishu app credentials yet. Run once: agent-lark setup',
  awayCreated: 'Created Feishu group "{name}"',
  awayReused: 'Took back Feishu group "{name}"',
  awayKept: 'Connected to Feishu group "{name}"',
  awayBoundChat: 'Bound to Feishu group {chatId}',
  awayOff: 'Remote mode is off.',
  awayOn: 'Remote mode is on: decisions, and moments when the agent is stuck on a prompt that needs you, are pushed to this project\'s Feishu group.',
  // status
  statusCredsYes: 'credentials: configured, from {origin}',
  statusCredsNo: 'credentials: not configured; run agent-lark setup first',
  statusHerdrIn: 'herdr: inside herdr, pane {pane}',
  statusHerdrOut: 'herdr: not inside herdr',
  statusDaemonDown: 'daemon: not running (agent-lark daemon --detach)',
  statusDaemonLine: 'daemon: pid {pid}, connected {connected}, connection {connection}, pending questions {pending}',
  statusNoBindings: 'bindings: none yet',
  statusBindings: 'bindings:',
  statusBindingLine: '  {mark} {root}  {name}  {chatId}  away={away}  pane={pane}',
  statusReleased: 'released (take one back with away on --reuse <chat_id>; * marks this project):',
  statusReleasedLine: '  {mark} {root}  {name}  {chatId}  released {time}',
  // ipc client / server
  ipcDaemonDown: 'daemon is not running. Start it first: agent-lark daemon --detach',
  ipcConnect: 'cannot connect to the daemon: {message}',
  ipcClosed: 'the daemon dropped the connection before answering (it may have crashed or been stopped)',
  ipcTimeout: 'the daemon did not respond in time',
  ipcBadRequest: 'unparseable request',
  ipcUnknownRequest: 'unknown request',
  // daemon replies
  notBound: 'this project is not bound yet; run agent-lark away on first',
  askPending: 'this project already has a question pending on the phone; one at a time',
  askNote: 'sent to the Feishu group, waiting for the answer (up to {seconds} s)',
  askTimedOut: 'no answer after {seconds} s',
  askCancelledStop: 'the daemon is stopping; the question was sent but no answer will arrive this time',
  askClientGone: 'the asking client disconnected',
  sendFailed: 'send failed: {error}',
  unbindNone: 'this project is not bound',
  unbindPending: 'a question is still pending on the phone; answer it or wait for the timeout',
  dissolveRefused:
    'the Feishu group "{name}" was not dissolved: Feishu answered {code} {msg}. Dissolve it by hand in Feishu (the app can only dissolve a group it owns, or one it created if it has the im:chat:operate_as_owner scope). The local record is removed.',
  dissolveThrew: 'the Feishu group "{name}" was not dissolved: {error}. Dissolve it by hand in Feishu. The local record is removed.',
  dissolveMarkerCleared: "The group's marker was cleared, so it will not be offered back.",
  dissolveMarkerKept: "The group's marker could not be cleared ({error}), so it will be offered back until it is dissolved.",
  dissolveOldDaemon:
    'the running daemon predates --dissolve and has only let the group go (it stays in Feishu, on record as released). Restart the daemon (agent-lark daemon --stop, then agent-lark daemon --detach), bind the group back (away on --reuse <chat_id>) and run unbind --dissolve again',
  bindNoOwner: 'nobody to invite into a new group (the app owner is not recorded). Use --chat <chat_id> to bind a group you created yourself.',
  bindCreateFailed:
    'creating the group failed: {error}\nIf this is a permission problem the app lacks the im:chat (create group) scope: run agent-lark setup --update, or bind an existing group with --chat <chat_id>.',
  fileMissing: 'file not found: {path}',
  fileRealpath: 'cannot resolve path: {error}',
  fileRefused:
    'refusing to send {real}\nOnly files under these directories can be sent:\n  this project {root}\n  {media}\n  {tmp}\n(this keeps send-file from reading arbitrary files off the machine)',
  fileNotRegular: 'not a regular file: {real}',
  fileTooBig: 'file too large: {size} MB, limit {cap} MB',
  // text synthesized into the pane
  injectVoice: '(voice transcript) {text}',
  injectUnheard:
    '({n} voice message(s) received but transcription failed — Feishu code {code}: {msg}. Either the app lacks the speech_to_text:speech scope (agent-lark setup --update adds it), or the tenant is on the free plan, which cannot call speech recognition at all. Tell the user to type it instead this time.)',
  injectNothingHeard: '({n} voice message(s) received but nothing was recognised in the audio. Tell the user to type it or send it again.)',
  injectSaved: '[saved: {path}]',
  replyTo: '(reply to: "{title}")',
  injectFilesWithText: '(attachments saved locally)',
  injectFilesOnly: '(I sent attachments; they are saved locally)',
  lateTapNoOption: '(follow-up) I tapped the card above again',
  latePick: '(follow-up) I pick {labels}',
  urgentNotSent: 'the urgent flag was not delivered ({error}); the question itself was sent and is waiting as usual',
  urgentNoOwner: 'the app owner is not recorded, so there is nobody to flag; rerun agent-lark setup --update to record it',
  urgentRefused: 'Feishu error {code} {msg}',
  // validation
  vTitleRequired: 'title: required and non-empty',
  vTitleTooLong: 'title: over {max} characters, got {n}',
  vTitleNewline: 'title: must be a single line',
  vFieldRequired: '{key}: required ({hint})',
  vFieldTooLong: '{key}: over {max} characters, got {n}',
  vHintDoing: 'one sentence: which task this is',
  vHintDescription: 'background for someone who has seen none of the work',
  vHintBlocker: 'exactly what is blocked',
  vHintReasoning: 'your leaning plus the strongest objection',
  vHintQuestion: 'a question answerable in one sentence',
  vOptionsArray: 'options: must be an array',
  vOptionsMin: 'options: at least {min} items (one option is not a choice)',
  vOptionsMax: 'options: at most {max} items (more means the question has not converged)',
  vOptionId: 'options[{i}].id: required',
  vOptionIdDup: 'options[{i}].id: "{id}" is duplicated',
  vOptionLabel: 'options[{i}].label: required (the button text, and what comes back when it is tapped)',
  vOptionLabelLong: 'options[{i}].label: over {max} characters',
  vOptionConsequence: 'options[{i}].consequence: required (what actually happens if chosen, including the cost)',
  vOptionConsequenceLong: 'options[{i}].consequence: over {max} characters',
  vRecommendRequired: 'recommend: required; the id of one option',
  vRecommendUnknown: 'recommend: "{id}" is not the id of any option',
  vRecommendDanger: 'recommend: "{id}" is marked danger; an irreversible or high-cost option cannot be the recommendation — list it and let the human choose',
  vRecommendString: 'recommend: with select "single" (the default) it must be one option id, a string — set select: "multi" to recommend several',
  vRecommendArray: 'recommend: with select "multi" it must be an array of option ids (strings)',
  vRecommendEmpty: 'recommend: with select "multi" the array must be non-empty — at least one option to tick by default',
  vRecommendDupItem: 'recommend: "{id}" is listed twice',
  vSelect: 'select: must be "single" or "multi", got {value}',
  vLang: 'lang: must be "zh" or "en", got {value}',
  vBodyRequired: 'body: required and non-empty',
  vBodyTooLong: 'body: over {max} characters',
  // credentials
  keychainDarwin: 'macOS Keychain',
  keychainWin32: 'Windows DPAPI (current-user encryption)',
  keychainLinux: 'libsecret (secret-tool)',
  credsPermWarning: 'agent-lark: warning: {file} permissions are too open ({mode}); chmod 600 recommended',
  credsNotPersisted: 'not persisted (memory only for this run)',
  originEnv: 'environment AGENT_LARK_APP_ID/SECRET',
  reportEnv: 'environment AGENT_LARK_APP_ID / AGENT_LARK_APP_SECRET',
  reportUnavailable: ' (not available on this machine)',
} as const;

export const t = (lang: Lang = 'zh'): Record<keyof typeof zh, string> => (lang === 'en' ? en : zh);

/** `{name}` → vars.name; placeholders without a value are left as they are. */
export function fill(template: string, vars: Record<string, string | number> = {}): string {
  return template.replace(/\{([a-zA-Z_]+)\}/g, (whole, name: string) => (name in vars ? String(vars[name]) : whole));
}

/** One `setup` line with both languages side by side; `varsEn` lets a substituted value differ per column. */
export function both(
  key: keyof typeof zh,
  vars: Record<string, string | number> = {},
  varsEn: Record<string, string | number> = vars,
): string {
  const z = fill(zh[key], vars);
  const e = fill(en[key], varsEn);
  // A multi-line value (the setup menu) reads as two blocks, one per
  // language; joining blocks on one line splices the last zh line onto the first en line.
  return z.includes('\n') || e.includes('\n') ? `${z}\n${e}` : `${z}　/　${e}`;
}
