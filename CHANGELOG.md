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

[Unreleased]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/jschollenberger/discord-asterisk-bridge/releases/tag/v1.0.0
