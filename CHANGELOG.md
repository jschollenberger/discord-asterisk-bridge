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

[Unreleased]: https://github.com/jschollenberger/discord-asterisk-bridge/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jschollenberger/discord-asterisk-bridge/releases/tag/v1.0.0
