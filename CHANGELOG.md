# Changelog

All notable changes to this repository are documented here, **one section per skill**. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); each skill is versioned on its own
([Semantic Versioning](https://semver.org/)) and released as a git tag `<skill>/vX.Y.Z` (`agent-lark/v0.1.0`,
`agent-ntfy/v0.1.3`, …). The tags `v0.1.0`–`v0.1.2` without a prefix predate the second skill and belong to
agent-ntfy's history. A skill is installed from the repository by content, so a version is only a tag you can
pin (`npx skills add 'yezhoujie/agent-ntfy-skill#v0.1.2' --skill agent-ntfy`).

## agent-lark

### [Unreleased][agent-lark-unreleased]

### [0.1.0][agent-lark-0.1.0] - 2026-09-15

First release of the Feishu / Lark channel: the same ask-and-block contract as agent-ntfy, carried by a
Feishu custom app of your own instead of a public notification service.

#### Added

- `ask`: one JSON on stdin becomes a Feishu card with one button per option; the reply (the tapped label, or
  whatever was typed in the group) comes back verbatim on stdout, exit codes 0 / 1 / 2 / 3 / 4 as in agent-ntfy.
  Irreversible options carry `danger: true` (red button behind a native confirm dialog) and can never be the
  recommendation; `select: "multi"` renders tick boxes plus a Submit button and returns the ticked labels joined
  with `、`; `--urgent` additionally flags the card to the app owner in-app (any refusal is only a `note:`);
  `lang` (`zh` / `en`, default `en`) selects the card's fixed wording. Answered cards turn green with the reply on
  top, timed-out and cancelled cards grey; the buttons lock on the first tap.
- `notify` (one-way card, Markdown body) and `send-file` (an image or a file into the project group, only from
  the project, the media directory or the temp directory; 10 MB images / 30 MB files).
- One Feishu group per project, named `<task> [<dir>]` and marked with the project path in its description.
  `away on --name "<task>"` starts the daemon, waits for Feishu, creates the group (or, on exit 4, lists the
  groups this project let go of earlier for `--reuse <chat_id>` / `--new`) and only then switches remote mode on;
  `rename "<task>"`, `unbind` (the group stays in Feishu and is offered back next time), `bind --chat <id>` for a
  group made by hand, `status` for the whole picture. Groups are found again from their description when the
  local records are gone.
- Phone → agent, with [herdr](https://herdr.dev): messages sent in the group while no question is pending are
  injected into the project's pane as `[agent-lark remote] …`, a `Get` reaction marks delivery, an orange receipt
  card says why when delivery is impossible; a Feishu reply to one of the skill's cards arrives as
  `(reply to: "<card title>")`; photos and files are downloaded and listed as `[saved: <path>]`; voice notes are
  transcribed (a failure is reported with Feishu's code and message instead of a placeholder). Attachments are kept
  under `~/.agent-lark/media/` and swept after `AGENT_LARK_MEDIA_TTL_DAYS` (7) days.
- The 🔔 *waiting for you* card: pushed while remote mode is on and herdr reports the session stuck on a prompt
  only a human can answer, at most once a minute per project.
- The daemon opens its local endpoint before the Feishu handshake and retries the handshake with backoff, so an
  offline machine never crash-loops; a Unix socket on macOS / Linux, a named pipe on Windows; `daemon --stop` is
  refused while a question is pending unless `--force`; `daemon --status` reports the connection and the media
  directory.
- `setup`: creates the Feishu app by QR code (retrying on network errors), or adopts an existing one with
  `--app-id` and the secret from the environment / env file; credentials go to the macOS keychain, a Windows
  DPAPI file, Linux `secret-tool`, or a `0600` file; `--update` re-authorizes, `--reset` starts over.
- Documentation for the agent (`SKILL.md`, `references/{daemon,failures,message-spec}.md`) and for people
  (`README.md`, `README.zh-CN.md`), plus a ready-made remote-mode rule in English and Chinese
  (`examples/remote-mode-rule*.md`).
- `node --test` suite (cards, validation, IPC, daemon with a fake Feishu channel, the CLI against a fake daemon)
  on ubuntu / windows / macos × Node 22 / 24.

## agent-ntfy

### [Unreleased]

### [0.1.2] - 2026-09-14

#### Added

- `confirm-sub --report-to <pane>`: a confirmation pane opened by the CLI sends its result (confirmed / timed
  out / interrupted / failed) back into the agent's session as one line prefixed `[agent-ntfy] `, so the
  agent no longer has to poll `slots`. Set automatically on auto-opened panes.
- A `confirm-sub` run by hand ends with a line to paste into the agent (`agent-ntfy: slotN is confirmed; …`)
  and tells the user the terminal window can be closed — the only way the result reaches an agent without herdr.

#### Changed

- `away on` leases a slot for the project on the spot (a confirmed idle slot if any, else the lowest
  unconfirmed idle one, which it then sends through the reachability check) instead of waiting for the first
  `ask`: the lease and the check are settled while the human is still at the keyboard, and `state.json` shows
  the slot right away. `away off` releases the project's slot. The daemon gained a `lease` command for this.
- A lease belongs to its project until that project releases it. When every slot is leased, `ask` / `notify`
  / `away on` no longer list "idle slots you could take over"; they print the occupancy (holder, idle or
  question pending, confirmed or not, one line per slot) for the user to decide between turning remote mode
  off in one of those projects and `add-slot`. `release <slot>` now carries the caller's project identity and
  refuses another project's slot (`not_yours`, rc 4); to free a lease whose project directory is gone, run it
  with `AGENT_NTFY_TARGET=<that holder>`. The state layer's `replace` (never reachable from the CLI) is gone.
- `away off` clears `slot` / `confirmed` in `state.json` only when the lease was actually released (or there
  was none); when the release is refused or the daemon is not running the file keeps saying what the daemon's
  lease says. It also no longer re-targets the injection pane.
- Restart the daemon after upgrading: `away on` now needs the daemon's `lease` command, which an older daemon
  does not know (stop it with its own CLI: `agent-ntfy daemon --stop`).

### [0.1.1] - 2026-09-14

#### Added

- `--lang zh|en` on the command line (a top-level option, before the subcommand). The wording language is now
  resolved once per process as `--lang` > `AGENT_NTFY_LANG` > the system locale (`LC_ALL` / `LC_MESSAGES` /
  `LANG` starting with `zh`, or a Chinese Windows locale, means `zh`) > `en`, so a machine with a Chinese locale
  gets Chinese wording without setting anything. Panes and daemons the CLI starts for you receive the resolved
  language via `--lang` (no more dependence on the POSIX `env` tool).
- The confirmation pane that `away on` / `confirm-sub` opens for you asks `Close this pane? [Y/n]` after a
  successful check and closes itself on Enter (`--close-pane`, set only on auto-opened panes; never asked
  after a failure, and never when stdout is not a terminal).
- README §2.2: iPhone users use the ntfy web app added to the home screen — the iOS App Store app receives
  notifications but has no reply box.
- `examples/remote-mode-rule.md` / `remote-mode-rule.zh-CN.md`: a ready-made standing rule for the agent
  (session start, the human leaving, decisions via `ask`, `notify` only for answers and major events, the
  human returning, teams of sessions), and README §13 on how to keep the skill in force for the whole session.

#### Changed

- The confirmation guide and the "tell the user" text warn not to close the pane before pressing Enter and
  tapping the button — closing it cancels the check.
- The receipt for a phone message on a slot whose lease has no pane names the commands that register one
  (`ask`, `notify`, `slots`, `release` without argument, `away on`, `away status`) instead of "any command".

#### Fixed

- One-shot commands (`notify`, `slots`, `release`, `add-slot`, `daemon --status|--stop`) give up after 60 s
  with "no response from the daemon" (exit 3) when the daemon accepted the connection but never answered,
  instead of hanging until Ctrl-C.
- The leases file is written through the same private atomic-write path as the topic pool file, so a leftover
  temporary file with wide permissions can no longer be renamed into place as is.

### [0.1.0] - 2026-09-13

First tagged release. Earlier commits were never versioned; the entries below describe what changed
relative to those untagged versions.

#### Added

- `notify` subcommand: a one-way notification to the same phone (`{"title", "body", "lang"?}` on stdin,
  Markdown body, no button, no waiting). Allowed while a question is pending; exit codes 0 sent · 1 invalid
  input · 3 channel failure · 4 a human must act. Anything the user sends from the phone afterwards counts as
  the reply to the pending question first.
- `away on` is a one-stop setup: it starts the daemon if none answers (inside herdr in a new pane, otherwise
  detached), makes sure a confirmed slot exists (starting the reachability check in a new pane when needed),
  then writes the per-project state file. `away status` checks the state file against the daemon's leases and corrects it.
- `confirm-sub` run by an agent inside herdr (stdout not a terminal) opens a pane for the user to do the
  check in, prints the pane id and exits 0 — the topic name stays in that pane.
- Linux and Windows: a loopback-TCP IPC transport (`AGENT_NTFY_IPC`, default on Windows) next to the Unix
  socket; the topic pool can live in a 0600 file or a Windows DPAPI-encrypted file (`AGENT_NTFY_STORE`);
  daemon `--detach` / `--status` / `--stop` no longer rely on POSIX-only calls. Unit tests run on GitHub
  Actions for ubuntu / windows / macos × Python 3.10 / 3.13, plus pyright. No end-to-end test on Linux or
  Windows yet.
- `daemon --status` shows the IPC transport in use.
- A phone message for a slot whose lease has no pane registered (the commands were run outside herdr) gets a
  receipt saying so, with `Release slot` / `Ignore` buttons, instead of a generic "not inside herdr" text.
- When the project root cannot be determined (the current directory was deleted), `ask` / `notify` / `slots` /
  `release` / `away` print a readable error and exit 3 instead of a traceback.
- Cards are rendered as Markdown: bold section labels, numbered options, a `---` rule before the closing
  hint.
- `LICENSE` (MIT) and this changelog.

#### Changed

- Messages sent from the phone while no question is pending are injected into the agent's session with the
  prefix `[agent-ntfy remote] ` in front (previously the text arrived without any marker); the text itself is
  unchanged.
- A slot is leased by the **project** (the git toplevel of the current directory, otherwise the directory
  itself) instead of by the herdr pane, so changing panes or sessions inside one project no longer burns a
  new slot. The lease additionally records the herdr pane from which the project last ran `ask`, `notify`, `slots`,
  `release` (no argument), `away on` or `away status`; phone messages are injected there. The `[tag]` in card titles is the project directory name. `release` with no argument
  releases the current project's slot. `AGENT_NTFY_TARGET` still overrides the identity.
- While remote mode is on (`away on`), `ask` and `notify` only use confirmed slots; they exit 4 with the
  candidates instead of leasing an unconfirmed one.
- `daemon --status` and `--stop` talk to the daemon over its socket (no signals). A daemon
  started by an earlier version does not understand the stop command: stop it with that version's CLI or
  `kill -TERM <pid>` before upgrading (see the README, "Versions and upgrading").
- Leases created by earlier versions (holder is a pane id) are kept and can only be released by hand
  (`release <slot>`). An earlier daemon ignores the new request fields, so the daemon must be restarted after
  upgrading.
- `daemon --detach`'s "not ready within 5 s" message no longer guesses at a keychain prompt; CLI wording and
  error texts are platform-neutral (e.g. the Unix-socket path-length hint no longer quotes a macOS-only
  number).

#### Fixed

- Option numbers on the card: the ntfy Android app renders Markdown ordered lists as bullets, so the options
  are now written with an escaped period (`1\.`) and separated by blank lines — CommonMark renders an escaped
  period as a plain `1.`, `2.` instead of a list.
- Windows: closing the subscription stream no longer waits for the stream timeout when the reader thread is
  blocked; `confirm-sub` no longer reports a channel failure (exit 3) when the daemon timed out while the
  user was still being asked to press Enter.
- A missing `security` command (the keychain exists only on macOS) is reported as such, with the
  `AGENT_NTFY_STORE` alternatives, instead of as a socket error.

[agent-lark-unreleased]: https://github.com/yezhoujie/agent-ntfy-skill/compare/agent-lark/v0.1.0...HEAD
[agent-lark-0.1.0]: https://github.com/yezhoujie/agent-ntfy-skill/releases/tag/agent-lark/v0.1.0
[Unreleased]: https://github.com/yezhoujie/agent-ntfy-skill/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/yezhoujie/agent-ntfy-skill/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/yezhoujie/agent-ntfy-skill/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/yezhoujie/agent-ntfy-skill/releases/tag/v0.1.0
