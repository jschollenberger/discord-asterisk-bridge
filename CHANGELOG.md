# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Note on version history.** Development before this first public release used
> an internal `7.x` numbering that was never cut as a git tag or release. That
> scheme is retired here: `1.0.0` is the first tagged, publicly released
> version. Some older commit messages and branch names still reference `7.1` —
> they refer to that pre-release internal numbering, not to any published
> release.

## [Unreleased]

## [1.1.5] — 2026-07-24

Bug-fix release.

### Fixed
- **Audio could go silent in Discord until a manual restart after a voice-server
  rotation.** When Discord dropped the voice WebSocket (close code 1006) and
  discord.py's reconnect stalled ("Could not connect to voice… Retrying…"), the
  connection stayed dead — no clean-disconnect event fired, so nothing noticed.
  The SIP side kept running (recordings/activity intact), so the bot looked
  healthy while producing no Discord audio. The watchdog now tracks the channel
  the bot intends to stream in and forces a full rejoin when the voice link is
  down, while still leaving a deliberate `/leave`, panel-stop, or admin kick
  alone. (#44)

## [1.1.4] — 2026-07-24

Accreditation and generic branding, plus bug fixes.

### Added
- **`/help` now carries project accreditation** — an "About" line (project name,
  version, author, GPLv3) plus **Source** and **Report an issue** link buttons
  to the GitHub repository.

### Changed
- **Generic branding.** The project is now "Discord Repeater Bot" (was named for
  the K2BR/SCARA deployment it was built for). The per-deployment display name
  shown in embeds/presence/console now uses the club's `name` from config (e.g.
  "SCARA Repeater Bot") rather than the callsign, so other clubs get their own.
  Internal logger names changed from `k2br.*` to `bot.*` and the QRZ user-agent
  is now generic. No config changes required.

### Fixed
- **Autocomplete could silently break with many configured commands.** Discord
  rejects an autocomplete response with more than 25 choices (the picker then
  shows nothing), and `/repeater-cmd`'s command list is bounded only by
  `repeater_commands` — which a club can easily configure past 25. All
  autocompletes now cap at 25 choices and clamp each choice's display name to
  Discord's 100-char limit. (#40)
- **`/repeater-status` could fail to render when a repeater was linked to a
  large number of nodes.** The "Repeaters" embed field listed every linked node
  with no length cap, and a single busy net could push it past Discord's
  1024-char per-field limit — which rejects the *whole* embed, so the command
  showed nothing. The linked-node list is now capped per repeater with a
  `… +N more` notice, plus a hard backstop on the field length. (#40)
- **The benign Windows/asyncio "connection lost" error is no longer logged as a
  scary `ERROR` + traceback.** When a proactor socket drops, asyncio calls
  `sock.shutdown()` on a socket Windows already considers invalid, raising
  `OSError` WinError 10022/10038 — harmless, since the connection is closing
  anyway. It's now demoted to a one-line DEBUG; every other asyncio error still
  surfaces normally. (#39)
- **A benign traceback was logged when a SIP call dropped via a remote BYE**
  (e.g. the far-end Asterisk restarting). The call moves to `ENDED` before the
  reconnect cleanup runs `call.hangup()`, and rfcvoip raises
  `InvalidStateError("Call is not answered")` on a non-answered call — which was
  caught but logged with a full traceback, making a normal, self-healing
  reconnect look like a crash. Cleanup now only hangs up a still-`ANSWERED`
  call. The reconnect itself was already working correctly. (#39)

## [1.1.3] — 2026-07-24

Bug-fix release.

### Fixed
- **Terminal dashboard's first live refresh was delayed ~1 s.** A regression
  from the v1.1.2 shutdown fix left the refresh worker waiting a second before
  its first repaint (it started with `stop.wait()` rather than painting first).
  The initial render still appeared at startup, but fresh data was a beat late;
  the worker now paints immediately again and still stops the instant shutdown
  is signalled. (#37)

### Internal
- Extracted the shutdown sequence into a module-level `_begin_shutdown()` with a
  testable ordering contract — the Live dashboard is torn down before any
  shutdown output — and added regression tests for it, the dashboard worker's
  prompt-stop/no-stray-repaint behavior, and the `_fmt_node_event` Discord
  message-length guard (an over-limit batch would make the whole post fail and
  silently drop the event). (#37)

## [1.1.2] — 2026-07-24

Bug-fix release.

### Fixed
- **Shutdown messages were hidden behind the terminal dashboard on Ctrl-C.** The
  Rich `Live` dashboard pins a render region to the bottom of the terminal and
  the worker repaints it every second, so the "Shutting down…" banner and the
  SIP-teardown logs were painted over and scrolled off-screen during discord.py's
  ~10–15 s voice-disconnect handshake — visible only if you scrolled back, which
  made it look like the shutdown message wasn't working. Shutdown now halts the
  dashboard worker and tears the Live region down *first*, so the banner and
  teardown logs print to a normal console. (#35)

## [1.1.1] — 2026-07-24

Bug-fix release — correctness fixes surfaced by an end-to-end code review,
plus the completion of the internal mypy type-cleanup.

### Fixed
- **Voice-channel listener tracking leaked entries when the bot left a
  channel.** A listener's join is recorded in `_voice_listeners` and popped
  when they leave — but that pop only fires while the bot is still in the
  channel. When the bot itself disconnected (`/leave`, panel stop, a true voice
  drop), those entries lingered for the process lifetime. `_clear_audio_client()`
  — which every disconnect path already routes through — now drops the departing
  guild's tracked listeners. (#32)
- **A SIP monitor could wedge itself after a failed registration or unanswered
  call.** `_connect_and_stream()` wrapped only the RX loop in `try/finally`, so
  two early exits after a successful `phone.start()` — a failed/timed-out
  registration and an unanswered call — never called `phone.stop()`. That
  leaked the bound local SIP port (rfcvoip binds without `SO_REUSEADDR`), so the
  next reconnect failed to rebind with `OSError` 10048/`EADDRINUSE` and looped
  until the process restarted. A single registration hiccup at boot was enough
  to trigger it. The teardown now spans the whole session, running on every exit
  path. (#31)
- **Control-panel "▶ Start" button could raise `AttributeError`.** It read
  `ix.member`, which `discord.Interaction` does not expose (there is no such
  attribute and no `__getattr__` fallback), so detecting the presser's voice
  channel crashed. It now narrows `ix.user` to a `Member`.
- **Panel-refresh method name collided with discord.py's internals.** The
  control panel defined `_refresh()`, shadowing `discord.ui.View._refresh()`
  (which discord.py calls internally with a different signature). Renamed to
  `_refresh_panel()`.

### Internal
- Completed the mypy burn-down of `allstar_discord_bot.py` (124 → 0 errors) and
  removed its per-module `disable_error_code` override, so the module is now
  gated on every error code like the rest of the codebase. Remaining
  framework-typing gaps use narrow inline `# type: ignore[code]` comments.

## [1.1.0] — 2026-07-24

Feature release — new operator-facing conveniences plus a command
consolidation and rename.

### Added
- **Voice channel status** — the voice channel's status line (shown under its
  name in the channel list) now reflects the live repeater: on-air state,
  repeater/frequency, and SIP health (e.g. `🔴 On the air · VHF 146.745`).
  Requires the optional "Set Voice Channel Status" permission; degrades
  gracefully without it. (#27)
- **Operator commands are hidden from the slash picker** for members without
  access, via per-command default permissions (map your operator role once in
  Server Settings → Integrations). `/help` still lists them. (#28)
- **`activity.hidden_nodes`** — node IDs to exclude from the linked-node
  display and the link/unlink activity feed (e.g. an internal EchoLink node). (#26)

### Changed
- **`/status` → `/repeater-status`** and **`/info` → `/repeater-info`.** The
  status command now also shows each repeater's linked AllStar nodes, so the
  separate **`/node-status` command is removed** (folded in). (#26)

### Fixed
- Linked-node parsing no longer drops legitimate 7+ digit private nodes or
  named SIP peers — it now excludes only the repeater's own node, the bot's
  own DISCORD connection, and `hidden_nodes`. (#26)

## [1.0.4] — 2026-07-23

Bug-fix and small-feature release.

### Fixed
- **`/solar` returned N/A for every field.** The parser looked for an RSS-style
  `<item>` element, but hamqsl.com nests everything under `<solar><solardata>…`;
  it now locates `<solardata>` (with `<item>` and the root as fallbacks). (#23)

### Added
- **Ctrl-C prints "Shutting down…" immediately** instead of leaving the console
  silent for ~10–15 s while discord.py's voice-disconnect handshake completes. (#23)
- **Voice-channel listener logging** — joins and leaves of the channel the bot is
  streaming into are logged to the console and log file (with time spent on
  leave); no Discord posting. (#24)
- **Bot presence shows the active repeater** — "Broadcasting `<repeater> <freq>`",
  updated on startup and every preset switch. (#24)

## [1.0.3] — 2026-07-22

Bug-fix release.

### Fixed
- **Discord "speaking" indicator no longer stays lit after a transmission.**
  AllStar's phone-mode call streams continuous RTP (~50 fps even in silence),
  so the playback buffer was refilled as fast as it drained and rarely reached
  empty — the pause fired only on a random timing coincidence (often long after
  the over, or not before the next one). Playback now stops enqueueing incoming
  frames once VAD declares end-of-transmission, so the buffered tail drains and
  the pause fires promptly (~1–2 s) and deterministically. (#21)
- **Dashboard "Stream Up" resets when switching presets** — it was counting
  from the original stream start (effectively bot uptime) rather than the
  current preset's stream. (#20)
- **Dashboard "Recon." counts only genuine reconnects** (auto-reconnect after a
  drop, watchdog recovery) — not user-initiated preset switches or manual
  `/reconnect`, which were inflating it while the most genuine event
  (auto-reconnect) wasn't counted at all. (#20)

### Added
- DEBUG-level instrumentation across the VAD → playback pause/resume cycle
  (start/end of transmission with RMS, buffer-drained pause request, and the
  bot-side resume/pause) — visible at `bot.log_file_level: DEBUG`. (#21)

## [1.0.2] — 2026-07-22

Bug-fix release.

### Fixed
- AMI `Command` actions that Asterisk rejects (`Response: Error` — e.g.
  permission denied, unknown command, or a node not local to the target box)
  now surface as an error instead of returning empty output that looked like
  success. `/repeater-cmd` reports the rejection with Asterisk's message rather
  than a false "ran successfully". (#18)
- Restored the commented-out per-repeater `discord:` placeholder in the `uhf`
  block of `config.example.yaml` — it was only present on `vhf`.

### Changed
- `/repeater-cmd` execution logging now names the concrete dispatch
  destination — target repeater, AllStar node, and AMI `host:port` — instead of
  the resolution provenance, so where a command actually went is visible at a
  glance. (#18)

## [1.0.1] — 2026-07-22

Maintenance release: no new features or breaking changes. Cleaner operational
logging, documentation, and internal type-safety/tooling work.

### Changed
- Routine Discord voice-server reconnects (WebSocket close code 1006) are now
  logged as a single plain-English INFO line instead of an ERROR with a stack
  trace — they're normal and self-healing on a long-running voice connection.
  A genuine burst of reconnects still escalates to WARNING with the traceback
  and a count, so a real problem stays loud. (#15)

### Documentation
- Added a "Creating the Discord bot" guide (Developer Portal setup, the exact
  OAuth scopes, and the minimal bot-permission list). (#10)
- Added `SECURITY.md` — private vulnerability reporting via GitHub advisories,
  operator responsibilities, and the dependency-audit posture. (#12)

### Fixed
- Corrected the `on_transmission` callback type annotation in
  `repeater_audio.py` (it was declared with one parameter but is documented and
  invoked with two). (#11)

### Internal
- Extracted the solar/HF-propagation fetch into its own `solar.py` module,
  mirroring `qrz.py`. (#13)
- Added mypy type checking to CI with an incremental adoption strategy, and
  began the type-cleanup of `allstar_discord_bot.py` (Batch 1: guild/channel
  Optional-narrowing). (#11, #14, #16)
- Ignore rotated test-log files (`test_run.log.N`) in git. (#9)

## [1.0.0] — 2026-07-19

First public release. Generalized from the K2BR (SCARA) deployment so other
clubs can run it against their own AllStar/HamVOIP nodes.

### Added
- **Low-latency RX** — direct SIP/RTP connection to Asterisk (via `rfcvoip`),
  no Icecast or FFmpeg; ~20 ms audio frames with minimal buffering.
- **TX (Discord → repeater)** — allowlisted users key the repeater by talking,
  gated by a per-repeater lock, a hard maximum transmission length, and an
  admin kill switch.
- **Always-on monitoring** — every configured repeater holds a persistent SIP
  connection from startup, independent of Discord voice; transmissions are
  VAD-detected, recorded, and logged on all repeaters full time.
- **Live connection health** — per-repeater SIP state surfaced in the control
  panel, `/status`, and the terminal dashboard.
- **Node activity feed** — AMI-polled AllStar link/unlink events, local-PTT
  activity, and SIP health alerts posted to Discord (batched, truncation-safe).
- **Repeater control from Discord** — `/link`, `/unlink`, `/unlink-all`,
  `/monitor-node`, `/link-repeaters` via the Asterisk Manager Interface.
- **Named HamVOIP actions** — `/repeater-cmd` exposes DTMF functions as
  autocompleted slash commands without raw DTMF access.
- **Ham radio utilities** — `/qrz` callsign lookup and `/solar` HF propagation
  report.
- **Per-repeater Discord channels** — each repeater can have its own voice and
  activity channel; with a second bot token ("satellite"), repeaters stream
  simultaneously rather than one-at-a-time.
- **Live terminal dashboard** (via `rich`) with a per-repeater row.

### Hardening / workarounds (see README "Known issues")
- Isolate `discord-ext-voice-recv` per-packet Opus decode failures so a single
  undecodable packet no longer kills the TX receive thread.
- Seed `rfcvoip` SIP Call-ID counters randomly per connection to avoid
  cross-restart identifier collisions (zombie-dialog remote-BYEs).

[Unreleased]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.1.5...HEAD
[1.1.5]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.1.4...v1.1.5
[1.1.4]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.1.3...v1.1.4
[1.1.3]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.0.4...v1.1.0
[1.0.4]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/jschollenberger/discord-asterisk-bridge/releases/tag/v1.0.0
