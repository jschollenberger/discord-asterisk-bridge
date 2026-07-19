# Repeater Discord Bot

[![CI](https://github.com/jschollenberger/discord-asterisk-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/jschollenberger/discord-asterisk-bridge/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

<!-- Replace jschollenberger/discord-asterisk-bridge above with your GitHub path once the repo is pushed. -->

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
- **Always-on monitoring** — every configured repeater holds a persistent
  SIP connection from startup, independent of Discord voice. Transmissions
  are detected, recorded, and logged on *all* repeaters full time, not just
  whichever one is currently audible.
- **Live connection health** — the control panel, `/status`, and the
  terminal dashboard show real SIP connection state per repeater
  (connected/reconnecting/etc), not just "is Discord playing something."
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
- **Per-repeater Discord channels** — each repeater can have its own voice
  channel and its own activity channel, and (with a second bot token) can
  stream simultaneously rather than one-at-a-time.
- **A live terminal dashboard** (via `rich`) with a row per repeater: SIP
  state, whether it's live in a voice channel or monitoring only, and
  recent log activity.

## Requirements

- Python 3.12+ (CI covers 3.12 and 3.13 on Linux, and 3.13 on Windows —
  which is what the reference deployment runs)
- An AllStar/HamVOIP node (or two) with AMI and SIP access
- A Discord bot application ([discord.com/developers/applications](https://discord.com/developers/applications))
  with the **Message Content** privileged intent enabled (needed for prefix
  commands), and voice permissions granted
- Optional: a [QRZ.com](https://www.qrz.com/) XML data subscription, for `/qrz`

All Python dependencies install from `requirements.txt`. Two of them —
`rfcvoip` and `discord-ext-voice-recv` — are pinned to exact versions
because this project patches and depends on their internals; see the
comments in that file before bumping either.

## Installation

```bash
git clone https://github.com/jschollenberger/discord-asterisk-bridge.git
cd discord-asterisk-bridge
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
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

For anything long-running, run it under a process supervisor
(systemd, [NSSM](https://nssm.cc/) on Windows) so it survives logout and
restarts on failure. Note the terminal dashboard only renders when run
interactively — as a service, the log file is your window in. Set
`bot.log_file_level: DEBUG` temporarily when diagnosing something.

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

**If you plan to use TX**, note that `P` (Phone Control mode) also defines
how the transmitter is keyed: audio sent into the call is *discarded* until
PTT is asserted in-band with DTMF `*99`, and `#` unkeys. The bot sends
these automatically when a TX lock is acquired and released, so operators
just talk — but a dialplan that hands the call to a bare `rpt(NODE)` will
answer, accept audio, and transmit nothing. Do **not** substitute `D`
("dumb phone mode") to avoid the DTMF: it keys the transmitter continuously
for the life of the call, and this bot holds its monitor calls open 24/7.

## Commands

Run `/help` in Discord for the full, current list. Commands that act on a
specific repeater take an optional `repeater` option; omit it and the bot
infers the target from the channel you ran it in (see
[Architecture notes](#architecture-notes)). Highlights:

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
- **`repeaters[].enabled`** (default `true`) takes a repeater fully offline
  — no SIP monitor, no recording, no activity posts, no health alerts.
  Useful when a node is down for maintenance, or staged in config before
  its Asterisk side is ready.
- **`bot.log_file_level`** — `INFO` for normal operation, `DEBUG` when
  chasing something. Steady-state SIP heartbeats are aggregated into one
  summary line per 10 minutes even at `DEBUG`, so the log stays readable.

## Known issues

- **`discord-ext-voice-recv` (TX) is labeled "Experimental" upstream.** Its
  packet router has no per-packet error handling: a single undecodable Opus
  packet — routine during Discord's key rotations — raises out of the
  decode call and kills the entire receive thread, silently disabling TX
  for the rest of the session. This bot patches `PacketDecoder.pop_data` at
  startup to isolate failures per packet (drop it, reset the decoder,
  resync), with the reattach watchdog kept as a backstop. The fragility is
  upstream; this is a workaround, which is why the package is pinned to an
  exact version.
- **`rfcvoip` reuses SIP identifiers across process restarts.** It derives
  Call-IDs from a counter that starts at 1 in every process, so a restart
  replays the same identifiers — which collides with any dialog Asterisk is
  still holding from an unclean shutdown, and gets the new call remote-BYE'd
  about a second after it answers. This bot seeds the counters randomly per
  connection to sidestep it. Setting `rtptimeout=60` in your `sip.conf` is a
  worthwhile belt-and-braces measure so stale dialogs self-clear.
- **`audioop` is deprecated** and was removed from the Python standard
  library in 3.13; the `audioop-lts` backport (pulled in automatically on
  3.13) covers it for now, but this is a dependency on a community
  maintained shim rather than the stdlib.
- **PyNaCl is held at a version with a known advisory.** `discord.py[voice]`
  constrains it to `PyNaCl<1.6`, which excludes the release that fixes
  PYSEC-2026-3002 — the upgrade is blocked upstream and can't be resolved
  here. CI's dependency audit suppresses that specific ID (so new advisories
  still surface) and it should be revisited whenever discord.py relaxes the
  cap.
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
per repeater in `config.yaml` (see `config.example.yaml` for full comments):

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

**Monitors vs. playback.** Each repeater gets exactly one persistent SIP
connection for the life of the process — not one per guild, and not one per
listener. Discord playback is a non-owning *view* onto that monitor, so
joining, leaving, or switching repeaters tears down only the view. This is
why activity logging and recording keep running on a repeater nobody is
currently listening to.

Because every monitor runs concurrently, each one binds its own local SIP
port (auto-assigned 5060, 5062, … unless pinned in config) and its own RTP
port range. Two clients sharing a local port is an immediate bind failure.

**Node link/unlink detection is AMI polling** (default every 15s), not a
live event stream — real-time enough for "someone linked up," not
millisecond-precise.

**TX targeting.** Commands that act on a repeater (`/link`, `/unlink`,
`/unlink-all`, `/monitor-node`, `/repeater-cmd`, `/tx-kill`) resolve their
target in three tiers: an explicit `repeater` option wins; otherwise the
invoking channel is matched against each repeater's voice and activity
channels (including threads under them); otherwise it falls back to the
guild's active repeater. Every reply states which repeater was chosen and
why, so a command can't quietly hit the wrong node.

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
