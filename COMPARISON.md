# agent-ntfy or agent-lark?

Both skills do the same job — the agent pushes a decision it cannot make to your phone, blocks, and gets your
verdict back on stdout; anything you send while no question is pending reaches the agent as an instruction — and
both expose the same command shape (`ask` with the same eight-field JSON, `notify`, `away on|off|status`, the
same exit codes 0–4). They differ in the channel, and everything below follows from that. Chinese version:
[COMPARISON.zh-CN.md](COMPARISON.zh-CN.md).

Sources, so you can check: agent-ntfy's [README](skills/agent-ntfy/README.md) (§2, §7, §8, §9) and
[SKILL.md](skills/agent-ntfy/SKILL.md); agent-lark's [README](skills/agent-lark/README.md) (§2, §9, §10) and
[SKILL.md](skills/agent-lark/SKILL.md).

## Pick in one minute

- You want it working in five minutes, with nothing to register: **agent-ntfy**. Install the ntfy app, subscribe to
  a topic, tap one test notification, done.
- You already live in Feishu / Lark, or you want photos, files and voice notes to reach the agent, more than one
  button, or a channel that is yours rather than a public server: **agent-lark**. One QR scan creates the app.
- iPhone: agent-lark. The ntfy iOS app receives notifications but has no reply box (agent-ntfy README §2.2 has the
  web-app workaround).

## Side by side

| | agent-ntfy | agent-lark |
|---|---|---|
| **Channel** | [ntfy.sh](https://ntfy.sh), a public push service (or your own ntfy instance, `AGENT_NTFY_URL`); one random topic per project out of a pool | A Feishu / Lark custom app of your own, one Feishu group per project |
| **What the phone needs** | The ntfy app; no account anywhere | Feishu / Lark, signed in with your own account — a personal account is enough, no workspace admin |
| **Runtime on the machine** | Python ≥ 3.10, standard library only | Node.js ≥ 22, one self-contained file |
| **First-time setup** | Subscribe the phone to the topic shown in the terminal, tap the button on a test notification (`confirm-sub`, once per topic) | `setup`: scan a QR code in the terminal with Feishu (or `--app-id` an app you already have); `away on` then creates the project's group and invites you |
| **Platforms** | macOS end to end; Linux and Windows unit tests on CI only | The same: macOS end to end (inside herdr); Linux and Windows unit tests on CI only |
| **herdr** | Optional: needed for phone → agent injection and for `away on` / `confirm-sub` opening panes | Optional: needed for phone → agent injection and for the 🔔 *waiting for you* card; everything else works without |
| **How you answer** | One button, "Accept recommended", or type in the topic | One button per option (2–5); irreversible options are red with a confirm dialog; `select: "multi"` gives tick boxes and a Submit button; or type in the group |
| **What comes back to the agent** | The recommended option's label on a tap, or your text | The tapped option's label, the ticked labels joined with `、`, or your text |
| **Agent → phone besides questions** | `notify` (title + body) | `notify` (title + body), `send-file` (an image or a file), `--urgent` on a question (Feishu's in-app urgent ping) |
| **Phone → agent besides text** | Text only | Photos and files (downloaded, path handed to the agent), voice notes (transcribed; needs a paid Feishu tenant), Feishu replies to a card arrive with that card's title |
| **Delivery feedback on the phone** | A receipt card only when a message could not be delivered | A `Get` reaction on every message that reached the terminal; a receipt card when it could not |
| **Stuck-agent alert** | None | 🔔 card when herdr reports the session blocked on a prompt (remote mode on, at most once a minute) |
| **Message size** | Card body ≤ 3584 bytes, title ≤ 960 bytes (ntfy limits) | Title ≤ 200 characters, each text field ≤ 4000, `notify` body ≤ 8000 |
| **Retention** | ntfy.sh caches a message 12 hours; a phone offline longer misses it (default `ask` timeout is 12 h for that reason) | The card is a message in the group; nothing expires on the channel side (default `ask` timeout is also 12 h) |
| **Quota** | About 250 messages per day per source IP on ntfy.sh, shared by questions, updates and receipts | No daily message quota; Feishu's API rate limits apply (not measured in normal use) |
| **What to keep secret** | The topic name — whoever knows it reads everything and can instruct the agent | The app secret (kept in the OS keychain; never in argv or output) — and group membership: anyone in the project's group can drive the agent |
| **Where content travels** | In clear through ntfy.sh | Through Feishu's servers; the group's description carries the project's absolute path |
| **Stopping the daemon with a question pending** | Stops; the waiting `ask` exits 3 | Refused; `--force` stops and cancels |
| **End of a task** | `release`: the slot goes back to the pool (`away off` does it too) | `unbind`: the group stays in Feishu and is offered back for renaming next time; `away off` only flips the switch |
| **Language of the fixed wording** | `lang` in the JSON; otherwise `--lang` / `AGENT_NTFY_LANG` / the system locale | `lang` in the JSON (default `en`); the CLI itself is English only |

## Both installed

They do not know about each other, and nothing in either decides which one a decision goes to. That is the job
of the standing rule you give the agent: keep one remote-mode rule in force per machine or per project, and let
it name the CLI it calls — [agent-ntfy's rule](skills/agent-ntfy/examples/remote-mode-rule.md) or
[agent-lark's rule](skills/agent-lark/examples/remote-mode-rule.md) (each has a Chinese twin next to it). Running
both daemons side by side is fine; they share nothing.
