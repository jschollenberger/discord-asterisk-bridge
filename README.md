# Repeater Discord Bot

[![CI](https://github.com/jschollenberger/discord-asterisk-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/jschollenberger/discord-asterisk-bridge/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

Streams an AllStar/HamVOIP repeater network into Discord voice channels over
direct SIP/RTP audio — no Icecast, no FFmpeg. 20ms audio frames with minimal
buffering, versus the ~30s buffer delay a typical Icecast relay carries.
Also supports two-way audio (Discord → repeater), AllStar link/unlink
control, a live node activity feed, QRZ.com callsign lookups, and solar/HF
propagation reports.

Originally built for the K2BR repeater system (SCARA), generalized here so
other clubs can run it against their own AllStar nodes.

## Features

- **Low-latency RX** — direct SIP/RTP connection to Asterisk, not a relayed
  Icecast stream. Discord hears the repeater in near real time.
- **TX (Discord → repeater)** — specific, allowlisted Discord users can key
  up the repeater by talking normally in the voice channel. Gated by a
  per-repeater lock (only one speaker at a time, matching real repeater
  behavior), a hard maximum transmission length, and an admin kill switch.
- **Live connection health** — the bot's control panel and `/status` show
  real SIP connection state (connected/reconnecting/etc), not just "is
  Discord playing something."
- **Node activity feed** — polls AllStar link state and posts link/unlink
  events to a Discord channel, batched and truncation-safe for busy hub
  nodes. Also posts local-PTT activity (who's transmitting, for how long)
  and SIP health alerts.
- **Repeater control from Discord** — `/link`, `/unlink`, `/unlink-all`,
  `/monitor-node`, `/link-repeaters` (bridge two repeaters together), all
  via the Asterisk Manager Interface (AMI).
- **Named HamVOIP actions** — `/repeater-cmd` exposes DTMF functions (time
  announcement, station ID, full status, etc.) as autocompleted slash
  commands, without giving raw DTMF access.
- **Ham radio utilities** — `/qrz` (callsign lookup), `/solar` (HF
  propagation/band conditions).
- **A live terminal dashboard** (via `rich`) showing per-guild streaming
  status, SIP connection state, and recent log activity.

## Requirements

- Python 3.10+ (developed and tested against 3.12)
- An AllStar/HamVOIP node (or two) with AMI and SIP access
- A Discord bot application ([discord.com/developers/applications](https://discord.com/developers/applications))
  with the **Message Content** privileged intent enabled (needed for prefix
  commands), and voice permissions granted
- Optional: a [QRZ.com](https://www.qrz.com/) XML data subscription, for `/qrz`
- Optional, only if you want two-way TX: the `discord-ext-voice-recv` package
  (see [Known issues](#known-issues) — it's labeled "Experimental" upstream)

## Installation

```bash
git clone <this repo>
cd <this repo>
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Then edit `config.yaml` — see the extensive inline comments in that file for
what each setting does and why. At minimum you'll need:

- Your Discord bot token
- Your AllStar node number(s) and AMI credentials
- SIP credentials for the direct RTP audio connection (see
  [Asterisk-side setup](#asterisk-side-setup) below — this part has a
  specific gotcha that will silently fail without it)

`config.yaml` is gitignored on purpose — it will contain live credentials.
Never commit it.

```bash
chmod 600 config.yaml
python allstar_discord_bot.py
```

## Asterisk-side setup

Each repeater needs a SIP peer and a dialplan context so this bot can
connect to it as a direct audio source. Add to **`sip_custom.conf`**:

```ini
[discord-vhf]              ; one section per repeater
type=friend
secret=CHOOSE_A_PASSWORD
host=dynamic
context=discord-audio
disallow=all
allow=ulaw
dtmfmode=rfc2833
qualify=yes
callerid="DISCORD" <YOUR_NODE_NUMBER>   ; optional — how this shows up in
                                          ; `core show channels`/CDR
```

And to **`extensions_custom.conf`**:

```ini
[discord-audio]
exten => YOUR_NODE_NUMBER,1,Answer()
exten => YOUR_NODE_NUMBER,n,Wait(1)
exten => YOUR_NODE_NUMBER,n,rpt(YOUR_NODE_NUMBER,P)
exten => YOUR_NODE_NUMBER,n,Hangup()
```

**The `,P` matters.** A bare `rpt(NODE)` runs in "normal endpoint mode,"
which verifies the connecting client's source IP against AllStar's node/IP
database — a check this bot will always fail, since it's a plain SIP client
authenticated via `secret=` above, not a registered AllStar peer node.
Without `P`, Asterisk answers the call and then immediately hangs up
(visible in the bot's log as a call that connects, receives one audio
frame, and ends within about a second). `P` ("Phone Control mode") is the
option that actually works for a non-node SIP client like this — confirmed
against real hardware, not just documentation.

Repeat both blocks per repeater, then:

```bash
asterisk -rx "sip reload"
asterisk -rx "dialplan reload"
```

## Commands

Run `/help` in Discord for the full, current list. Highlights:

| Command | Description |
|---|---|
| `/join`, `/leave` | Join your voice channel and start/stop streaming |
| `/vhf`, `/uhf`, `/stream <name>` | Switch the active repeater |
| `/panel` | Post the interactive control panel (buttons, no typing needed) |
| `/status`, `/info` | Live status / static repeater info card |
| `/link <node>`, `/unlink <node>`, `/unlink-all` | AllStar link control (operator role) |
| `/link-repeaters`, `/unlink-repeaters` | Bridge/unbridge two of your repeaters |
| `/node-status` | Live AMI query of connected nodes |
| `/repeater-cmd <name>` | Run a named HamVOIP action (autocomplete) |
| `/tx-status`, `/tx-kill` | TX lock status / emergency force-release (operator role) |
| `/qrz <callsign>` | QRZ.com lookup |
| `/solar` | Solar flux, K/A index, HF band conditions |

## Configuration reference

`config.example.yaml` is the actual documentation for every setting — each
one has a comment explaining what it does, why the default is what it is,
and (for the site-dependent ones like VAD thresholds) how to tune it. A few
things worth knowing going in:

- **`activity.vad_rms_threshold`** and **`activity.vad_hangover_seconds`**
  are genuinely site-dependent (your receiver's audio level, your
  operators' speech patterns) — there's no universally-correct default.
  Expect to tune these by watching the log while people key up.
- **`tx.enabled`** defaults to `false`. Read the block of comments above it
  in the example config before turning it on — it covers what the bot can
  and can't enforce about who's allowed to transmit.
- **`repeater_commands`** DTMF codes are common HamVOIP defaults, not
  verified defaults for *your* node — AllStar function tables are
  per-node config, not a fixed standard. Check your own `rpt.conf`
  `[functionsNNNNN]` stanza before trusting any of them.

## Known issues

- **Intermittent SIP registration failures on Windows.** The underlying SIP
  library (`rfcvoip`) occasionally hits `OSError: [WinError 10038] An
  operation was attempted on something that is not a socket` during
  registration or call setup. It's intermittent — most sessions connect
  cleanly, some hit a retry storm that usually (not always) recovers via
  the built-in exponential backoff. This appears to be a Windows-specific
  socket-teardown timing issue in the library, not something fixable from
  this bot's code; running on Linux (native or WSL2) is the most reliable
  way to sidestep it entirely if you hit this regularly.
- **`discord-ext-voice-recv` (TX) is labeled "Experimental" upstream.** A
  single corrupted Opus packet from *any* speaker in the channel can crash
  its internal packet router, which would otherwise silently disable TX for
  the rest of the session. This bot detects that and automatically
  reattaches (capped at 5 consecutive recovery attempts), but the
  underlying fragility is in the library, not something this bot's code can
  fully fix.
- **`rfcvoip` is GPLv3-licensed.** This project is itself GPLv3 (see
  [License](#license)), so the dependency's terms are fully compatible
  however you install or redistribute it.

## Always-on monitoring

Every repeater with `sip_audio` configured gets a persistent SIP monitor
from the moment the bot starts, independent of Discord voice: transmissions
on every repeater are VAD-detected, recorded (if enabled), and posted to
that repeater's activity channel full time. A Discord voice connection is
just a playback view onto one of these monitors — joining, leaving, or
switching presets never interrupts monitoring, and `/status` (and the
control panel) shows each repeater as either 🔊 live in a voice channel or
👂 monitoring only. The one thing a single bot application can't do is be
*audible* in two voice channels at once — that's what satellite tokens are
for, below.

## Multiple repeaters, multiple channels

Repeaters can each be bound to their own Discord voice channel — and
optionally their own Discord application — via an optional `discord:` block
per repeater in `config.yaml` (see `config_example.yaml` for full comments):

- **No `discord:` blocks** — classic setup: one bot, one shared channel,
  `/vhf` / `/uhf` switch the single live stream. Existing configs work
  unchanged.
- **`channel_id` only** — each repeater has its own channel on the one bot;
  switching presets moves the bot between channels. Still one live stream
  at a time, because Discord permits one voice connection per guild per
  bot application.
- **`activity_channel_id`** — the repeater's activity posts
  (transmissions, node link/unlink, SIP health alerts, TX events) go to
  its own text channel; repeaters without one fall back to the global
  `activity.channel_id`. Works independently of the voice settings.
- **`token` + `channel_id`** — the repeater runs on a dedicated second
  Discord application as a headless "satellite" streamer inside the same
  process. Satellites join their channel at startup and stream
  simultaneously with the primary — VHF and UHF live in two channels at
  once. All commands, the control panel, and TX remain on the primary bot;
  `/status` shows satellite SIP state, and the SIP health watch covers
  them.

To add a satellite: create a second application + bot in the Discord
Developer Portal, invite it to your server with voice permissions, then set
its token and channel on the repeater's `discord:` block.

## Architecture notes

- `allstar_discord_bot.py` — the bot itself: commands, control panel,
  background tasks, TX lock/gating logic.
- `repeater_audio.py` — the SIP/RTP client (via `rfcvoip`). Owns the
  Asterisk connection, audio resampling in both directions, and
  energy-threshold voice-activity detection for the RX side.
- `ami.py` — Asterisk Manager Interface client (link/unlink/monitor via
  `ilink`, node status polling, the activity-feed monitor).
- `qrz.py` — QRZ.com XML API client.
- `config.py` / `config.yaml` — typed config loading; see
  `config.example.yaml` for the full reference.

Each repeater gets its own direct SIP connection per active Discord guild.
Node link/unlink detection is via AMI polling (default every 15s), not a
live event stream — real-time enough for "someone linked up," not
millisecond-precise.

## Development

Tests live in `tests/` (pytest) and run against a sanitized fixture config —
no Discord connection, SIP endpoint, or real credentials are needed. They
cover the behaviors that were debugged live and must not regress: VAD
drain-then-pause and buffer flushing, non-owning playback sources leaving
monitors running, PTT DTMF wiring, unique SIP/RTP port allocation,
signal-then-join shutdown, command target resolution (explicit / channel /
thread / fallback tiers), the voice_recv decoder hardening, the CryptoError
log rate-limiter, and dashboard rendering.

```
pip install -r requirements-dev.txt
pytest          # run the suite
ruff check .    # lint (pyflakes + error-class rules; style rules are out of scope)
```

CI (`.github/workflows/ci.yml`) runs on every push and pull request, plus
weekly on a schedule: ruff, a test matrix covering Ubuntu (3.12, 3.13) and
Windows (3.13 — what production actually runs on), and a `pip-audit`
dependency vulnerability scan. Dependabot opens weekly dependency PRs so
the exact pins below get bumped deliberately, with CI proving each one. Note that `rfcvoip` and
`discord-ext-voice-recv` are pinned exactly in `requirements.txt` because
this project patches and reaches into their internals — see the comments
there for what to re-test when bumping either.

## License

GPLv3 — see [`LICENSE`](LICENSE).

Copyright (C) 2026 Jason Schollenberger / KD2QED

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by the
Free Software Foundation, either version 3 of the License, or (at your
option) any later version. It is distributed WITHOUT ANY WARRANTY; see
the GNU General Public License for details.

This also aligns cleanly with the `rfcvoip` GPLv3 dependency noted above —
bundled or not, the whole project is under compatible terms.
