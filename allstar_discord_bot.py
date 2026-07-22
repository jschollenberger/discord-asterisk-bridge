"""
K2BR Repeater Bot
Copyright (C) 2026 Jason Schollenberger / KD2QED

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

K2BR Repeater Bot  v1.0.2
Streams the K2BR AllStar repeater network into Discord voice/stage channels
over direct SIP/RTP audio (see repeater_audio.py) — no FFmpeg or Icecast.

Setup:
    pip install "discord.py[voice]" pyyaml aiohttp rich rfcvoip

Configuration:  edit config.yaml
Run:            python allstar_discord_bot.py

Commands (slash  /cmd  or prefix  !cmd):
    /join  /leave  /status  /panel  /stream  /reconnect  /presets  /info
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ami import AMIClient, AMICommandError, monitor as node_monitor
from config import cfg, REPEATER_COMMAND_PLACEHOLDER, BOT_VERSION
from qrz import QRZClient, QRZError
from solar import fetch_solar

# QRZ client (None if not configured)
_qrz: Optional[QRZClient] = (
    QRZClient(cfg.qrz.username, cfg.qrz.api_key) if cfg.qrz else None
)

BOT_NAME = f"{cfg.club.callsign} Repeater Bot"

# ─────────────────────────────────────────────────────────────────────────────
# Per-Guild State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GuildState:
    """All mutable runtime state scoped to one Discord guild."""
    streaming:   bool            = False
    preset:      str             = field(default_factory=lambda: cfg.default_preset)
    started_at:  Optional[float] = None        # unix timestamp stream began
    reconnects:  int             = 0           # count of GENUINE reconnects only
                                                # (auto-reconnect after a drop, watchdog
                                                # recovery) — NOT user preset switches or
                                                # manual /reconnect. A rising count is the
                                                # at-a-glance signal of real voice instability.
    channel:     str             = "—"


_guild_states: dict[int, GuildState] = {}

def get_state(guild_id: int) -> GuildState:
    """Return the GuildState for the given guild, creating it if needed."""
    if guild_id not in _guild_states:
        _guild_states[guild_id] = GuildState()
    return _guild_states[guild_id]


# Registry of each guild's live SIP audio client, so status displays can query
# real connection state (CONNECTED / RECONNECTING / etc.) instead of guessing
# from gs.streaming alone. Populated by _make_source(), cleared on stop/leave.
# TYPE_CHECKING-only import avoids a hard dependency on repeater_audio/rfcvoip
# at module load time if that package isn't installed yet.
if TYPE_CHECKING:
    from repeater_audio import RepeaterAudioClient

# Always-on SIP monitors, one per repeater with sip_audio configured —
# started at boot (see _ensure_monitors) and running for the process
# lifetime regardless of what's being played to Discord. Every repeater's
# transmissions are therefore detected, recorded, and posted to its
# activity channel full time; a Discord voice connection is just a playback
# *view* onto one of these monitors (RepeaterAudioSource(owns_client=False)),
# so attaching/detaching playback never interrupts monitoring.
_monitor_clients: dict[str, "RepeaterAudioClient"] = {}   # rpt_id → SIP client

# Satellite bots — repeaters with a dedicated Discord app token run as
# headless SatelliteBot instances (see class SatelliteBot) playing their
# repeater's monitor client into their own channel.
_satellites: dict[str, "SatelliteBot"] = {}   # rpt_id → bot


# Global bot-start timestamp (process lifetime, not per guild)
_bot_started: float = datetime.now(timezone.utc).timestamp()
_global_cmds_purged: bool = False   # see on_ready — one-shot stale-command purge
_loop: Optional[asyncio.AbstractEventLoop] = None

# ─────────────────────────────────────────────────────────────────────────────
# Rich Console & Logging
# ─────────────────────────────────────────────────────────────────────────────

console = Console()

LOG_RING: deque[str] = deque(maxlen=15)

_LEVEL_COLOR = {
    "DEBUG":    "dim",
    "INFO":     "green",
    "WARNING":  "yellow",
    "ERROR":    "red",
    "CRITICAL": "bold red",
}

class RingLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        ts    = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        color = _LEVEL_COLOR.get(record.levelname, "white")
        LOG_RING.append(
            f"[dim]{ts}[/dim]  [{color}]{record.levelname:<8}[/{color}] {record.getMessage()}"
        )

def _setup_logging() -> logging.Logger:
    from pathlib import Path

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)          # capture everything; handlers filter down

    # ── Screen: INFO+ with logger name prefix ─────────────────────────────────
    rich_handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        show_path=False,
        markup=True,
    )
    rich_handler.setLevel(logging.INFO)
    rich_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root.addHandler(rich_handler)

    # ── File: DEBUG, everything, rotate on startup, keep 2 backups ────────────
    fh = RotatingFileHandler(
        cfg.bot.log_file,
        maxBytes=10_000_000,   # 10 MB per file
        backupCount=2,
        encoding="utf-8",
        delay=True,            # don't open until first write
    )
    # Rotate before the first write so each bot run starts in a fresh file
    log_path = Path(cfg.bot.log_file)
    if log_path.exists() and log_path.stat().st_size > 0:
        fh.doRollover()
    fh.setLevel(getattr(logging, cfg.bot.log_file_level, logging.DEBUG))
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)-20s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    # ── Dashboard ring ────────────────────────────────────────────────────────
    # Same INFO threshold as the console — without this, high-frequency
    # DEBUG-level tracing (SIP connection tracing, the rfcvoip debug bridge)
    # floods this 15-entry ring and pushes out the actually-useful INFO
    # messages, since this handler previously had no level filter at all.
    ring_handler = RingLogHandler()
    ring_handler.setLevel(logging.INFO)
    root.addHandler(ring_handler)

    # Suppress discord heartbeat/gateway spam (WARNING+ still reaches file).
    # discord.ext.voice_recv (the TX voice-receive library) belongs here too
    # — its router/reader submodules dump full RTCP packet contents (raw
    # bytes included) roughly once per second for the entire time a call is
    # connected. In one ~3-hour session this alone was over 94% of the
    # entire log file's volume (12,583 of 13,336 lines), dwarfing even the
    # SIP connection tracing this suppression list was originally added for.
    # Setting the level on the parent logger cascades to its child loggers
    # (.reader, .router, .gateway, .voice_client) the same way it already
    # does for discord.gateway's children — none of them set their own
    # explicit level, so they inherit this one.
    class _CryptoErrorRateLimit(logging.Filter):
        """
        Demote voice_recv's per-packet "CryptoError decoding packet data"
        spam. The library catches the error and DROPS the packet — receive
        continues fine — but logs each one at ERROR. Undecryptable packets
        are routine (Discord re-keys on speaker join/leave, stray packets
        during key rotation); they became visible once the decoder
        hardening stopped junk packets from killing/recreating the reader.
        Policy: swallow repeats, emit one WARNING summary per 60s window
        with a count of how many were suppressed.
        """
        WINDOW = 60.0

        def __init__(self):
            super().__init__()
            self._last  = 0.0
            self._count = 0

        def filter(self, record: logging.LogRecord) -> bool:
            if "CryptoError decoding packet data" not in record.getMessage():
                return True
            now = time.time()
            self._count += 1
            if now - self._last >= self.WINDOW:
                n, self._count, self._last = self._count, 0, now
                record.levelno   = logging.WARNING
                record.levelname = "WARNING"
                record.msg  = (f"voice_recv: {n} undecryptable packet(s) dropped in the last "
                               f"{int(self.WINDOW)}s (routine during Discord re-keys; harmless)")
                record.args = ()
                return True
            return False

    logging.getLogger("discord.ext.voice_recv.reader").addFilter(_CryptoErrorRateLimit())

    class _SipHeartbeatAggregator(logging.Filter):
        """
        Collapse rfcvoip's steady-state heartbeat one-liners. With two
        always-on monitors, "Method: OPTIONS" + 2× "Status: 200 OK" +
        "New register thread" repeat every ~2 minutes per monitor — ~30k
        lines/day of pure "still fine" that drowns the file log at DEBUG.

        Policy: swallow exactly those three messages, counting them, and
        emit ONE summary line per window. Anything else from k2br.sip —
        non-200 statuses, unexpected methods (MESSAGE bursts around
        activity, BYE, INVITE), state transitions, errors — passes through
        untouched and immediately, because deviation from the heartbeat is
        precisely what's diagnostic.
        """
        WINDOW = 600.0   # one summary line per 10 minutes
        _HEARTBEATS = {
            "[rfcvoip] Method: OPTIONS":    "OPTIONS",
            "[rfcvoip] Status: 200 OK":     "200 OK",
            "[rfcvoip] New register thread": "register",
        }

        def __init__(self):
            super().__init__()
            self._counts: dict[str, int] = {}
            self._window_start = time.time()

        def filter(self, record: logging.LogRecord) -> bool:
            label = self._HEARTBEATS.get(record.getMessage())
            if label is None:
                return True
            self._counts[label] = self._counts.get(label, 0) + 1
            now = time.time()
            if now - self._window_start >= self.WINDOW:
                counts, self._counts = self._counts, {}
                mins = int((now - self._window_start) / 60)
                self._window_start = now
                summary = ", ".join(f"{n}× {k}" for k, n in sorted(counts.items()))
                record.msg  = f"SIP heartbeat OK — {summary} in last {mins}min"
                record.args = ()
                return True
            return False

    logging.getLogger("k2br.sip").addFilter(_SipHeartbeatAggregator())

    class _VoiceReconnectFilter(logging.Filter):
        """
        Tame discord.voice_state's routine reconnect churn. Discord rotates a
        long-running voice connection between its own voice servers, closing
        the old WebSocket with code 1006 (or letting it time out) and handing
        the bot a fresh endpoint; discord.py reconnects transparently within a
        second or two and playback resumes on its own (our _after_play /
        watchdog backstops never even fire). Over a multi-day run that's ~one
        cycle every few hours — each an identical ConnectionClosed traceback
        logged at ERROR, which reads like a crash to anyone who doesn't know
        it's normal.

        Policy:
          - "Disconnected from voice…": drop the (always-identical, non-
            diagnostic) traceback and demote to a single plain-English INFO
            line that says it's routine and self-healing. BUT if these cluster
            — >= THRESHOLD within WINDOW, i.e. a retry storm or a genuinely
            failing reconnect rather than normal server rotation — keep it at
            WARNING, RETAIN the traceback, and report the count so an actual
            problem stays unmistakably loud.
          - The interim handshake chatter (Connecting/Starting/complete/
            terminated): demote to DEBUG so it's out of the way at INFO but
            still there when log_file_level is DEBUG for troubleshooting.
          - "Voice connection complete." and anything else: pass through
            untouched, so recovery stays visible and any unexpected
            voice_state message keeps its original level.
        """
        WINDOW    = 600.0   # 10 minutes
        THRESHOLD = 5       # reconnects within WINDOW before we escalate

        _HANDSHAKE = (
            "Connecting to voice",
            "Starting voice handshake",
            "Voice handshake complete",
            "The voice handshake is being terminated",
        )

        def __init__(self):
            super().__init__()
            self._events: deque[float] = deque()

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()

            if msg.startswith("Disconnected from voice"):
                exc    = record.exc_info[1] if record.exc_info else None
                code   = getattr(exc, "code", None)
                reason = f"close code {code}" if code is not None else "connection timeout"

                now = time.time()
                self._events.append(now)
                while self._events and now - self._events[0] > self.WINDOW:
                    self._events.popleft()
                n = len(self._events)

                record.args = ()
                if n >= self.THRESHOLD:
                    # Abnormal: stays loud and keeps the traceback for diagnosis.
                    record.levelno   = logging.WARNING
                    record.levelname = "WARNING"
                    record.msg = (f"Voice link dropped ({reason}) — {n} reconnects in the last "
                                  f"{int(self.WINDOW / 60)} min. That's more than routine server "
                                  f"rotation; if it keeps up, check the host's network or Discord's "
                                  f"voice status.")
                else:
                    # Routine: one clean INFO line, no alarming traceback.
                    record.levelno   = logging.INFO
                    record.levelname = "INFO"
                    record.exc_info  = None
                    record.exc_text  = None
                    record.msg = (f"Voice link dropped ({reason}) and auto-reconnecting — routine "
                                  f"for a long-running Discord voice connection, not an error.")
                return True

            if msg.startswith(self._HANDSHAKE):
                record.levelno   = logging.DEBUG
                record.levelname = "DEBUG"
                return True

            return True

    logging.getLogger("discord.voice_state").addFilter(_VoiceReconnectFilter())

    for noisy in ("discord.gateway", "discord.client", "discord.http", "discord.ext.voice_recv"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("k2br")

log = _setup_logging()

# ─────────────────────────────────────────────────────────────────────────────
# Terminal Dashboard
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_uptime(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    s = int(datetime.now(timezone.utc).timestamp() - ts)
    return f"{s // 3600:02d}h {(s % 3600) // 60:02d}m {s % 60:02d}s"

def build_dashboard() -> Layout:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Repeater status table ──
    # One row per CONFIGURED repeater, always — the always-on monitor model
    # means every repeater has a SIP state worth showing from boot, whether
    # or not it's connected yet or being played anywhere. Playback details
    # (channel, uptime, reconnects) come from whichever guild is currently
    # playing that repeater; monitor-only repeaters show their monitoring
    # role with playback columns dimmed.
    gtbl = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", expand=True)
    gtbl.add_column("Repeater",   min_width=14)
    gtbl.add_column("SIP",        min_width=15)   # longest: "🟠 Reconnecting…"
    gtbl.add_column("Role",       min_width=14)   # longest: "👂 Monitoring 🛰"
    gtbl.add_column("Where",      min_width=26)
    gtbl.add_column("Stream Up",  min_width=12)
    gtbl.add_column("Recon.",     min_width=6)

    for r in cfg.repeaters:
        client = _monitor_clients.get(r.id)
        if client is not None:
            sip_str = _STATE_DISPLAY.get(client.state.name, client.state.name)
        elif not r.enabled:
            sip_str = "[dim]⏸ Disabled[/dim]"
        elif r.sip_audio:
            sip_str = "[dim]⚫ Not started[/dim]"
        else:
            sip_str = "[dim]— no sip_audio[/dim]"

        vc_r = _playback_vc_for(r.id)
        sat  = " 🛰" if r.discord.is_dedicated(cfg.bot.token) else ""
        if vc_r is not None:
            role  = f"[green]🔊 Live[/green]{sat}"
            where = vc_r.channel.name[:30]
            gs    = get_state(vc_r.guild.id)
            up    = _fmt_uptime(gs.started_at) if gs.streaming else "—"
            recon = str(gs.reconnects)
        else:
            role  = f"👂 Monitoring{sat}" if client is not None else "[dim]—[/dim]"
            where = "[dim]—[/dim]"
            up    = "[dim]—[/dim]"
            recon = "[dim]—[/dim]"

        gtbl.add_row(
            f"{r.id} [dim]({r.frequency_mhz:.3f})[/dim]",
            sip_str,
            role,
            where,
            up,
            recon,
        )

    if not cfg.repeaters:
        gtbl.add_row("[dim]No repeaters configured[/dim]", "", "", "", "", "")

    guild_panel = Panel(
        gtbl,
        title=f"[bold magenta]📻 {BOT_NAME}[/bold magenta]  [dim]{now}  ·  bot up {_fmt_uptime(_bot_started)}[/dim]",
        border_style="magenta",
    )

    # ── Log panel ──
    log_lines = "\n".join(LOG_RING) if LOG_RING else "[dim]No logs yet.[/dim]"
    log_panel  = Panel(Text.from_markup(log_lines), title="[bold]Recent Logs[/bold]", border_style="blue")

    layout = Layout()
    layout.split_column(
        Layout(guild_panel, size=8 + max(len(cfg.repeaters), 1)),
        Layout(log_panel),
    )
    return layout

# ─────────────────────────────────────────────────────────────────────────────
# Audio Helpers
# ─────────────────────────────────────────────────────────────────────────────

class NoAudioConfiguredError(Exception):
    """Raised when a repeater has no sip_audio block configured in config.yaml."""


def _ensure_monitors() -> None:
    """
    Idempotently create and start the always-on SIP monitor for every
    repeater that has sip_audio configured. Called once the shared event
    loop exists (on_ready / _run_all_bots); safe to call repeatedly.

    From this point on, every repeater is being monitored full time:
    transmissions are VAD-detected, recorded (if enabled), and posted to
    the repeater's activity channel whether or not anything is playing it
    to a Discord voice channel. Playback attaches as a non-owning view
    (see _make_source) and never interrupts monitoring.
    """
    from repeater_audio import RepeaterAudioClient

    # Unique LOCAL network resources per concurrent client — two clients
    # binding the same local SIP port is an instant OSError 10048/EADDRINUSE
    # reconnect loop. Explicit config (sip_audio.local_sip_port) wins;
    # otherwise auto-assign 5060, 5062, ... skipping anything taken. RTP
    # ranges are split into disjoint 2000-port blocks per repeater for the
    # same reason (collisions there are rarer but just as real).
    used_ports = {r.sip_audio.local_sip_port
                  for r in cfg.repeaters
                  if r.sip_audio and r.sip_audio.local_sip_port}
    def _next_sip_port() -> int:
        p = 5060
        while p in used_ports:
            p += 2
        used_ports.add(p)
        return p

    for idx, rpt in enumerate(cfg.repeaters):
        if not rpt.enabled:
            log.info(f"SIP monitor skipped: '{rpt.id}' is disabled in config "
                     f"(enabled: false) — no connection, recording, or alerts.")
            continue
        if not rpt.sip_audio or rpt.id in _monitor_clients:
            continue
        sip_port = rpt.sip_audio.local_sip_port or _next_sip_port()
        rtp_low  = 10000 + idx * 2000
        rtp_high = rtp_low + 1999
        client = RepeaterAudioClient(
            host                       = rpt.sip_audio.host,
            port                       = rpt.sip_audio.port,
            username                   = rpt.sip_audio.username,
            password                   = rpt.sip_audio.password,
            extension                  = rpt.sip_audio.extension,
            local_ip                   = rpt.sip_audio.local_ip,
            on_transmission            = _make_on_transmission(rpt),
            on_speaking_change         = _make_on_speaking_change(rpt.id),
            on_drained                 = _make_on_drained(rpt.id),
            vad_rms_threshold          = cfg.activity.vad_rms_threshold,
            vad_hangover_seconds       = cfg.activity.vad_hangover_seconds,
            local_sip_port             = sip_port,
            ptt_key_dtmf               = cfg.tx.ptt_key_dtmf,
            ptt_unkey_dtmf             = cfg.tx.ptt_unkey_dtmf,
            rtp_port_low               = rtp_low,
            rtp_port_high              = rtp_high,
            sip_debug_verbose          = cfg.bot.sip_debug_verbose,
            record_transmissions       = cfg.activity.record_transmissions,
            max_recording_seconds      = cfg.activity.max_recording_seconds,
        )
        _monitor_clients[rpt.id] = client
        client.start()
        log.info(f"SIP monitor started (always-on): '{rpt.id}' → "
                 f"{rpt.sip_audio.host} (local SIP :{sip_port}, RTP {rtp_low}-{rtp_high})")


def _stop_monitors() -> None:
    """
    Stop every always-on monitor — process shutdown only. Two phases:
    signal all clients first (so their SIP teardowns proceed in parallel),
    THEN join each worker thread. Without the join, process exit kills the
    daemon workers mid-teardown and whichever monitor hadn't sent its BYE
    yet leaves a zombie call on Asterisk — seen live as "VHF disconnects
    cleanly, UHF never does."
    """
    for rpt_id, client in list(_monitor_clients.items()):
        try:
            client.stop()
        except Exception:
            log.debug(f"monitor stop failed [{rpt_id}]", exc_info=True)
    for rpt_id, client in list(_monitor_clients.items()):
        try:
            if client.wait_stopped(6.0):
                log.info(f"SIP monitor '{rpt_id}' shut down cleanly.")
            else:
                log.warning(
                    f"SIP monitor '{rpt_id}' did not finish SIP teardown within 6s — "
                    f"its call may linger on Asterisk until rtptimeout clears it."
                )
        except Exception:
            log.debug(f"monitor join failed [{rpt_id}]", exc_info=True)
    _monitor_clients.clear()


def _make_source(
    preset_id: str,
    guild_id: Optional[int] = None,
    bot_obj: Optional[discord.Client] = None,
) -> discord.AudioSource:
    """
    Return a playback source for the given repeater: a NON-OWNING
    RepeaterAudioSource over the repeater's always-on monitor client.
    Stopping/replacing playback (vc.stop(), preset switch, leave) tears
    down only this view — the underlying SIP monitor keeps running, so
    activity detection and recording continue uninterrupted.

    guild_id / bot_obj are accepted for call-site compatibility but no
    longer used: monitors are keyed purely by repeater, and the VAD
    callbacks resolve their playback target dynamically via
    _playback_vc_for().

    Raises NoAudioConfiguredError if the repeater doesn't exist or has no
    sip_audio configured.
    """
    rpt = cfg.repeater_by_id(preset_id)
    if not rpt or not rpt.sip_audio:
        raise NoAudioConfiguredError(
            f"Repeater '{preset_id}' has no sip_audio configured in config.yaml."
        )
    if not rpt.enabled:
        # No monitor exists for a disabled repeater, so there is nothing to
        # attach playback to — surface it as the same clean error rather
        # than a KeyError from _monitor_clients below.
        raise NoAudioConfiguredError(
            f"Repeater '{preset_id}' is disabled in config.yaml (enabled: false)."
        )
    _ensure_monitors()
    client = _monitor_clients[preset_id]
    # Start playback at real time: while monitor-only, nothing drains the
    # buffer, so it always holds a full deque of stale audio by now.
    client.flush_buffer()
    from repeater_audio import RepeaterAudioSource
    log.debug(f"Attached playback view to monitor '{preset_id}'")
    return RepeaterAudioSource(client, owns_client=False)


def _play_paused(vc: discord.VoiceClient, source: discord.AudioSource, after) -> None:
    """
    Start playback but immediately pause — every call site that used to call
    vc.play(_make_source(...)) directly should use this instead.

    Without this, the bot shows as "speaking" in Discord from the instant
    it connects until the first real burst of repeater activity completes a
    full VAD cycle — discord.py's AudioPlayer marks speaking=True the
    moment .play() starts, and nothing turns it back off until our VAD-driven
    on_speaking_change callback (see _make_on_speaking_change) sees real
    audio, then silence. Pausing immediately after play() means playback
    starts in the correct "not speaking" state instead, and resume() (driven
    by that same VAD callback) takes over cleanly the moment real activity
    is detected — Discord never sees a sustained "speaking" state with
    nothing behind it.
    """
    vc.play(source, after=after)
    vc.pause()
    # If we're attaching to a monitor that's mid-transmission (e.g. someone
    # switched presets while the other repeater was active), resume right
    # away — the VAD's speaking edge already fired and won't fire again
    # until the next transmission starts.
    client = getattr(source, "_client", None)
    if client is not None and getattr(client, "voice_active", False):
        try:
            client.flush_buffer()
            vc.resume()
        except Exception:
            log.debug("mid-transmission resume failed", exc_info=True)


_STATE_DISPLAY = {
    "IDLE":         "⚪ Idle",
    "CONNECTING":   "🟡 Connecting…",
    "CONNECTED":    "🟢 Connected",
    "RECONNECTING": "🟠 Reconnecting…",
    "STOPPED":      "⚫ Stopped",
}


def _audio_state_text(guild_id: int) -> Optional[str]:
    """
    Display string for the SIP state of the repeater this guild is set to
    play (its preset's always-on monitor), or None if no monitor exists.
    """
    client = _monitor_clients.get(get_state(guild_id).preset)
    if client is None:
        return None
    return _STATE_DISPLAY.get(client.state.name, client.state.name)


def _clear_audio_client(guild_id: int) -> None:
    """
    Drop the guild's audio client registration on explicit stop/leave, and
    release any TX lock the departing connection was relaying for — an
    orphaned lock with no live connection to relay through would just sit
    there confusing the next /status until its hangover/timeout expired
    anyway, so release it immediately instead.
    """
    gs = get_state(guild_id)
    lock = _tx_locks.pop(gs.preset, None)
    if lock is not None:
        _tx_release_ptt(gs.preset)
        log.info(f"TX: released {lock.callsign} on {gs.preset} (guild disconnect)")
    # NOTE: the repeater's SIP monitor deliberately keeps running — leaving
    # the voice channel stops playback, not monitoring/recording.


def _playback_vc_for(rpt_id: str) -> Optional[discord.VoiceClient]:
    """
    Return the voice connection currently *playing* this repeater, or None
    if the repeater is monitor-only right now. Satellites play their own
    repeater on their own client; on the primary, a guild plays whatever
    its preset is set to. This is the gate that lets every repeater's
    always-on monitor run VAD/recording full time while only the repeater
    actually attached to a voice connection drives that connection's
    pause/resume (speaking indicator).
    """
    sat = _satellites.get(rpt_id)
    if sat is not None:
        for g in sat.guilds:
            if g.voice_client is not None:
                return g.voice_client
        return None
    for g in bot.guilds:
        if get_state(g.id).preset == rpt_id and g.voice_client is not None:
            return g.voice_client
    return None


def _make_on_speaking_change(rpt_id: str):
    """
    Build the on_speaking_change(active) callback for a repeater's
    always-on monitor. Only the True edge acts, and only when this repeater
    is what a voice connection is currently playing (see _playback_vc_for)
    — otherwise transmissions are still detected/recorded/posted, they just
    don't touch any voice connection.

    Tail-cutoff design (introduced v6.7): pausing happens on the drained edge, not
    here — see _make_on_drained. flush_buffer() before resume keeps
    playback at real time instead of resuming from a backlog of buffered
    idle silence (while paused or monitor-only, nothing drains the buffer,
    so it always holds ~2s of stale audio by the time a transmission
    starts).

    Runs on the SIP thread; pause/resume are synchronous and internally
    thread-safe (a threading.Event under the hood), and no coroutine is
    scheduled, so no run_coroutine_threadsafe is needed here.
    """
    def _callback(active: bool) -> None:
        vc = _playback_vc_for(rpt_id)
        if vc is None or not vc.is_connected():
            return
        try:
            if active and vc.is_paused():
                client = _monitor_clients.get(rpt_id)
                if client is not None:
                    client.flush_buffer()   # resume at real time, not 2s behind
                vc.resume()
                log.debug(f"Playback resumed [{rpt_id}] — transmission started")
            # False edge: intentionally no vc.pause() here — deferred to
            # on_drained so the buffered tail plays out first.
        except Exception:
            log.debug(f"speaking-indicator resume failed [{rpt_id}]", exc_info=True)
    return _callback


def _make_on_drained(rpt_id: str):
    """
    Build the on_drained() callback for a repeater's monitor. Fires on
    discord.py's audio-player thread once the RX buffer has fully drained
    after a VAD end-of-transmission — the correct moment to pause playback
    (the buffered tail has played out). No-op while the repeater is
    monitor-only. vc.pause() is thread-safe; calling it from the player
    thread is fine.
    """
    def _callback() -> None:
        vc = _playback_vc_for(rpt_id)
        if vc is None or not vc.is_connected():
            return
        try:
            if vc.is_playing():
                vc.pause()
                log.debug(f"Playback paused [{rpt_id}] — transmission ended (buffer drained)")
        except Exception:
            log.debug(f"drained-pause failed [{rpt_id}]", exc_info=True)
    return _callback


def _make_on_transmission(rpt):
    """
    Build the on_transmission(duration, recording) callback passed into
    RepeaterAudioClient. Called from the client's background thread (see
    repeater_audio.py's VAD), so it hops back onto the bot's event loop the
    same way _after_play does for playback-end callbacks, rather than
    touching discord.py directly from a non-asyncio thread.
    """
    def _callback(duration: float, recording: Optional[bytes]) -> None:
        if _loop is None:
            return
        asyncio.run_coroutine_threadsafe(_post_transmission_activity(rpt, duration, recording), _loop)
    return _callback


def _activity_channel(rpt) -> Optional[discord.abc.Messageable]:
    """
    Resolve the activity text channel for a repeater (RepeaterConfig or
    None): the repeater's own discord.activity_channel_id if configured,
    else the global activity.channel_id. All activity posting goes through
    the primary bot regardless of which app carries the audio — the primary
    is in the guild and can see every text channel, and one poster keeps
    permissions simple.
    """
    ch_id = cfg.activity_channel_id_for(rpt)
    return bot.get_channel(ch_id) if ch_id else None


async def _post_transmission_activity(rpt, duration: float, recording: Optional[bytes] = None) -> None:
    """
    Post a completed-transmission event to the activity channel, if
    configured — with the recorded audio attached as a WAV file when
    activity.record_transmissions is enabled (recording will be None
    otherwise; see repeater_audio.py's record_transmissions param).
    """
    channel = _activity_channel(rpt)
    if not channel:
        return
    text = (
        f"🎙️ **Local transmission** on **{rpt.display_name}** "
        f"({rpt.frequency_mhz:.3f} MHz) — {duration:.1f}s"
    )
    try:
        if recording:
            filename = f"{rpt.id}_{int(time.time())}.wav"
            await channel.send(text, file=discord.File(io.BytesIO(recording), filename=filename))
        else:
            await channel.send(text)
    except Exception as exc:
        log.warning(f"Activity channel post failed (VAD) [{rpt.id}]: {exc}")


# Tracks which guilds we've already posted a "stuck RECONNECTING" alert for,
# so sip_health_watch() doesn't repost every loop tick — only on the state
# change into "alerting" and back into "recovered". See sip_health_watch().
_sip_alerted: dict[int | str, bool] = {}   # guild_id or 'sat:<rpt_id>'


# ─────────────────────────────────────────────────────────────────────────────
# TX (Discord → repeater) — see config.yaml's "tx:" / "tx_operators:" sections
# for the safety model this implements. Short version: only Discord user IDs
# in tx_operators can transmit; only one holds a given repeater's lock at a
# time; a stuck-open transmission is force-released after
# cfg.tx.max_transmission_seconds regardless of continued activity.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TxLock:
    holder_user_id: int
    callsign:        str
    started_at:      float
    last_packet_at:  float


# Keyed by REPEATER id (rpt.id), not guild_id — the lock protects a shared
# physical resource (the transmitter), not a per-guild concept. With only one
# active guild today this distinction is dormant, but it's the correct scope
# regardless: two guilds both live on the same repeater must still share one
# lock, the same way two people can't both key up the same real radio.
_tx_locks: dict[str, TxLock] = {}

# Throttles the "unauthorized user attempted to transmit" log line so a
# non-operator talking continuously doesn't flood the log at packet rate.
_tx_unauthorized_warned: dict[int, float] = {}

# Same idea, but for "tried to transmit while SIP isn't connected" — keyed
# by repeater id rather than user id, and separate from the dict above since
# they throttle logically distinct situations.
_tx_no_connection_warned: dict[str, float] = {}


def _tx_assert_ptt(rpt_id: str) -> None:
    """Key the repeater's transmitter (app_rpt phone-mode DTMF *99) when a
    TX lock is acquired. Without this, app_rpt answers the call and takes
    our audio frames but never transmits them — confirmed live: frames
    were sent and nothing appeared on the Asterisk side as received TX."""
    client = _monitor_clients.get(rpt_id)
    if client is not None:
        client.key_ptt()


def _tx_release_ptt(rpt_id: str) -> None:
    """Unkey (DTMF #). Called on every TX release path so a lock can never
    end with the transmitter still keyed from our side."""
    client = _monitor_clients.get(rpt_id)
    if client is not None:
        client.unkey_ptt()


def _tx_try_relay(guild_id: int, rpt_id: str, user_id: int, pcm_48k_stereo: bytes) -> None:
    """
    Core TX gate. Called from the voice-receive sink's write() callback —
    i.e. from a background thread, at up to ~50 calls/sec while someone's
    talking — so this stays cheap (dict lookups only) and never blocks.

    Acquires the repeater's lock for the first authorized speaker heard,
    refreshes it on continued packets from the current holder, and silently
    drops audio from anyone else — same behavior as a real repeater when a
    second station keys up over an already-active one.

    Also gated on the repeater's SIP connection actually being CONNECTED —
    without this, a lock could be acquired (and a "keyed up" activity
    message posted) while the underlying SIP call is down, since
    send_frame() already no-ops safely in that case but says nothing about
    it. That gave a false impression of a successful transmission when
    nothing was actually reaching the repeater — observed directly in a
    real session where the SIP link never got past RECONNECTING the whole
    time TX events were being posted.
    """
    op = cfg.tx_operator_by_discord_id(user_id)
    if op is None:
        now = time.time()
        if now - _tx_unauthorized_warned.get(user_id, 0) > 30:
            _tx_unauthorized_warned[user_id] = now
            log.warning(f"TX: unauthorized Discord user {user_id} attempted to transmit on {rpt_id}")
        return

    client = _monitor_clients.get(rpt_id)
    # .state.name (string compare) avoids importing ConnectionState in this
    # packet-rate hot path — see repeater_audio.py for the enum itself.
    connected = client is not None and client.state.name == "CONNECTED"

    now  = time.time()
    lock = _tx_locks.get(rpt_id)

    if lock is None:
        if not connected:
            now2 = time.time()
            if now2 - _tx_no_connection_warned.get(rpt_id, 0) > 10:
                _tx_no_connection_warned[rpt_id] = now2
                log.warning(
                    f"TX: {op.callsign} tried to transmit on {rpt_id} but its SIP "
                    f"connection isn't up — not relaying, not reporting keyed"
                )
            return
        lock = TxLock(holder_user_id=user_id, callsign=op.callsign, started_at=now, last_packet_at=now)
        _tx_locks[rpt_id] = lock
        log.info(f"TX: {op.callsign} keyed up {rpt_id}")
        _tx_assert_ptt(rpt_id)
        if _loop:
            asyncio.run_coroutine_threadsafe(_post_tx_event(rpt_id, op.callsign, "keyed"), _loop)
    elif lock.holder_user_id == user_id:
        if not connected:
            # Connection dropped mid-transmission — release rather than
            # silently keep "holding" a lock that can't relay anything.
            _tx_locks.pop(rpt_id, None)
            _tx_release_ptt(rpt_id)   # call may already be dead; harmless no-op then
            log.warning(f"TX: {lock.callsign} lost SIP connection on {rpt_id} mid-transmission — released")
            if _loop:
                asyncio.run_coroutine_threadsafe(_post_tx_event(rpt_id, lock.callsign, "released"), _loop)
            return
        lock.last_packet_at = now
    else:
        return   # someone else already holds the lock — drop this packet

    if client is not None:
        client.send_frame(pcm_48k_stereo)


async def _post_tx_event(rpt_id: str, callsign: str, kind: str) -> None:
    """kind: 'keyed' | 'released' | 'timeout'"""
    rpt     = cfg.repeater_by_id(rpt_id)
    channel = _activity_channel(rpt)
    if not channel:
        return
    name = rpt.display_name if rpt else rpt_id
    text = {
        "keyed":    f"🔴 **{callsign}** keyed up **{name}**",
        "released": f"⚪ **{callsign}** released **{name}**",
        "timeout":  f"⏱️ **TX timeout** — **{callsign}** force-released from **{name}** "
                    f"after {cfg.tx.max_transmission_seconds}s",
    }[kind]
    try:
        await channel.send(text)
    except Exception as exc:
        log.warning(f"Activity channel post failed (TX {kind}): {exc}")


_tx_import_warned = False
_vr_decoder_hardened = False
_vr_drop_log_ts: dict[int, float] = {}   # ssrc → last time we logged a drop


def _harden_voice_recv_decoder() -> None:
    """
    Make discord-ext-voice-recv's per-packet Opus decode failures non-fatal.

    Upstream design flaw (confirmed live 2026-07-18, and by reading
    router.py/opus.py in 0.5.x): PacketRouter._do_run() calls
    decoder.pop_data() with no per-packet error handling, and run() catches
    everything at thread scope — so a SINGLE undecodable packet (Discord
    silence/FEC frames, DAVE/E2EE-wrapped audio, RTX, transient garbage)
    raises OpusError("corrupted stream"), kills the entire receive thread,
    and detaches the sink. Our reattach watchdog then loses the race: each
    fresh pipeline dies on the very next bad packet, burning all 5 attempts
    inside two seconds while a legitimate TX is in progress.

    Fix: wrap PacketDecoder.pop_data to catch per-packet failures, reset()
    the decoder (fresh Opus state machine, so it resyncs cleanly after
    corrupt input), drop the offending packet, and keep the thread alive.
    Good packets before and after flow through untouched — this is exactly
    the behavior an RTP receiver is supposed to have. Logged at WARNING but
    rate-limited to one line per SSRC per 10s so a sustained stream of
    undecodable packets (e.g. an E2EE-only participant) can't flood the log.
    """
    global _vr_decoder_hardened
    if _vr_decoder_hardened:
        return
    try:
        from discord.ext.voice_recv.opus import PacketDecoder
    except ImportError:
        return   # voice_recv not installed; nothing to harden

    _orig_pop_data = PacketDecoder.pop_data

    def _safe_pop_data(self, *, timeout: float = 0):
        try:
            return _orig_pop_data(self, timeout=timeout)
        except Exception as exc:
            now  = time.time()
            last = _vr_drop_log_ts.get(self.ssrc, 0.0)
            if now - last >= 10.0:
                _vr_drop_log_ts[self.ssrc] = now
                log.warning(
                    f"TX: dropped undecodable voice packet(s) from ssrc={self.ssrc} "
                    f"({type(exc).__name__}: {exc}) — receive pipeline continues"
                )
            try:
                self.reset()   # fresh Opus decoder state; resync on next good packet
            except Exception:
                pass
            return None        # router treats None as "nothing to deliver"

    PacketDecoder.pop_data = _safe_pop_data
    _vr_decoder_hardened = True
    log.info("TX: voice_recv decoder hardened (per-packet error isolation).")


def _get_voice_recv_client_cls():
    """
    Lazily import discord-ext-voice-recv's VoiceRecvClient for TX. Returns
    None (and warns once, not on every join attempt) if the package isn't
    installed, so the bot still joins/streams normally — just without TX —
    rather than crashing.
    """
    global _tx_import_warned
    try:
        from discord.ext.voice_recv import VoiceRecvClient
        _harden_voice_recv_decoder()
        return VoiceRecvClient
    except ImportError:
        if not _tx_import_warned:
            _tx_import_warned = True
            log.warning(
                "tx.enabled is true in config.yaml but discord-ext-voice-recv "
                "isn't installed — TX will not be available this run. "
                "Install it with: pip install discord-ext-voice-recv"
            )
        return None


def _make_tx_sink(guild_id: int):
    """
    Build a discord-ext-voice-recv AudioSink that feeds Discord mic audio
    into _tx_try_relay(). Lazy-imports voice_recv so the bot still runs with
    TX disabled (or the package not installed) — see _do_join's TX setup.
    """
    from discord.ext import voice_recv

    class RepeaterTxSink(voice_recv.AudioSink):
        def wants_opus(self) -> bool:
            return False   # decoded PCM, same convention as the RX side

        def write(self, user, data) -> None:
            if user is None or not data.pcm:
                return
            gs = get_state(guild_id)
            _tx_try_relay(guild_id, gs.preset, user.id, data.pcm)

        def cleanup(self) -> None:
            pass

    return RepeaterTxSink()


# discord-ext-voice-recv's PacketRouter._do_run() has no per-packet exception
# handling — a single corrupted Opus frame from ANY speaker in the channel
# (not just an authorized TX operator) propagates uncaught, and its outer
# handler calls voice_client.stop_listening() in a finally block, killing
# the entire TX receive pipeline for the rest of the session. Confirmed by
# reading the installed package's actual router.py, not assumed — this is a
# real gap in that library, not something in our own sink code (the crash
# happens in decode, upstream of our write() ever being called). Cap
# auto-recovery attempts so a persistently corrupted stream doesn't spin.
MAX_TX_RECOVERY_ATTEMPTS = 5


def _make_tx_listen_after(guild_id: int, attempt: int = 0):
    """
    Callback passed to vc.listen(sink, after=...). AudioReader.stop() calls
    this reliably whenever the TX receive pipeline stops, passing the
    exception that killed it (or None for an intentional stop, e.g. /leave —
    nothing to recover from in that case). On a crash, reattach a fresh sink
    automatically so a single bad Opus frame doesn't silently disable TX for
    the rest of the session.
    """
    def _callback(error: Optional[Exception]) -> None:
        if error is None:
            return   # intentional stop — not a crash, nothing to recover
        if attempt >= MAX_TX_RECOVERY_ATTEMPTS:
            log.error(
                f"TX: receive pipeline crashed {attempt} times in a row [{guild_id}] "
                f"— giving up auto-recovery. Last error: {error!r}. "
                f"A /leave and /join will re-arm it."
            )
            return
        log.warning(
            f"TX: receive pipeline crashed [{guild_id}] (attempt {attempt + 1}/"
            f"{MAX_TX_RECOVERY_ATTEMPTS}), reattaching: {error!r}"
        )
        if _loop is not None:
            asyncio.run_coroutine_threadsafe(_reattach_tx_sink(guild_id, attempt + 1), _loop)
    return _callback


async def _reattach_tx_sink(guild_id: int, attempt: int) -> None:
    """Re-arm TX receive after a crash — see _make_tx_listen_after()."""
    guild = bot.get_guild(guild_id)
    vc = guild.voice_client if guild else None
    voice_recv_cls = _get_voice_recv_client_cls()
    if vc is None or voice_recv_cls is None or not isinstance(vc, voice_recv_cls):
        return
    if not vc.is_connected() or vc.is_listening():
        return
    try:
        vc.listen(_make_tx_sink(guild_id), after=_make_tx_listen_after(guild_id, attempt))
        log.info(f"TX: receive pipeline reattached [{guild_id}]")
    except Exception as exc:
        log.error(f"TX: failed to reattach receive pipeline [{guild_id}]: {exc}")


async def _verify_tx_operators_via_qrz() -> None:
    """
    Sanity-check each configured tx_operators callsign against QRZ at
    startup. Warns on mismatch or lookup failure; never blocks bot startup
    or gates transmission — a QRZ outage or rate limit shouldn't be able to
    silently disable everyone's ability to transmit. Run as a background
    task (see on_ready) rather than awaited inline, so a slow/unreachable
    QRZ doesn't delay the rest of startup.
    """
    if not cfg.tx.verify_with_qrz_on_startup:
        return
    if _qrz is None:
        if cfg.tx_operators:
            log.warning(
                "tx.verify_with_qrz_on_startup is true but QRZ isn't configured "
                "(add qrz: to config.yaml) — skipping tx_operators verification."
            )
        return

    for op in cfg.tx_operators:
        try:
            data = await _qrz.lookup(op.callsign)
            looked_up = (data.get("callsign") or "").upper()
            if looked_up != op.callsign.upper():
                log.warning(
                    f"TX operator callsign mismatch: config.yaml has "
                    f"{op.callsign!r}, QRZ returned {looked_up!r} — check for a typo."
                )
            else:
                log.info(f"TX operator verified via QRZ: {op.callsign}")
        except QRZError as exc:
            log.warning(f"TX operator {op.callsign!r} failed QRZ verification: {exc}")
        except Exception as exc:
            log.warning(f"TX operator {op.callsign!r} QRZ verification error: {exc}")


def _after_play(error: Optional[Exception], vc: discord.VoiceClient) -> None:
    if error:
        log.error(f"Playback error [{vc.guild.name}]: {error}")
    if vc.is_connected() and _loop:
        asyncio.run_coroutine_threadsafe(_auto_reconnect(vc), _loop)


async def _auto_reconnect(vc: discord.VoiceClient) -> None:
    await asyncio.sleep(2)
    if vc.is_connected() and not vc.is_playing() and not vc.is_paused():
        gs = get_state(vc.guild.id)
        try:
            _play_paused(vc, _make_source(gs.preset, vc.guild.id), after=lambda e: _after_play(e, vc))
            if not gs.started_at:
                gs.started_at = datetime.now(timezone.utc).timestamp()
            gs.reconnects += 1   # genuine reconnect — playback dropped and we recovered it
            log.info(f"Auto-reconnected [{vc.guild.name}] (#{gs.reconnects})")
        except Exception as exc:
            log.error(f"Auto-reconnect failed [{vc.guild.name}]: {exc}")

# ─────────────────────────────────────────────────────────────────────────────
# Satellite Bots  (dedicated-token repeaters)
# ─────────────────────────────────────────────────────────────────────────────

class SatelliteBot(discord.Client):
    """
    Headless Discord client for one repeater with a dedicated application
    token (repeater.discord.token in config.yaml differing from bot.token).

    Discord permits one voice connection per guild per application, so the
    primary bot can only ever carry one live stream per guild. Each
    SatelliteBot is a second application that joins its repeater's
    configured voice channel and streams just that repeater — this is what
    makes simultaneous VHF + UHF channels in the same guild possible.

    Deliberately minimal: no commands, no control panel, no TX
    (voice-receive stays on the primary — satellite TX is a future step).
    All operator control still goes through the primary bot's commands;
    satellites play their repeater's always-on monitor client (see
    _monitor_clients), which /status and the SIP health watch already
    cover.
    """

    def __init__(self, rpt) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.rpt = rpt

    async def on_ready(self) -> None:
        log.info(f"Satellite ready: {self.user} → repeater '{self.rpt.id}'")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"{self.rpt.display_name} {self.rpt.frequency_mhz:.3f} MHz",
            )
        )
        await self._join_and_stream()

    async def _join_and_stream(self) -> None:
        ch_id = self.rpt.discord.channel_id
        ch = self.get_channel(ch_id)
        if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            log.error(
                f"Satellite '{self.rpt.id}': channel_id={ch_id} is not a "
                f"voice/stage channel this app can see — check the config and "
                f"that the app was invited to the server."
            )
            return
        try:
            vc = ch.guild.voice_client
            if vc is not None and not vc.is_connected():
                # Stale client from a dropped session — clear it out so we
                # do a genuinely fresh connect instead of operating on a
                # dead connection below.
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                vc = None
            vc = vc or await ch.connect(timeout=15, reconnect=True)
            if vc.channel != ch:
                await vc.move_to(ch)
            if isinstance(ch, discord.StageChannel):
                try:
                    await ch.guild.me.edit(suppress=False)
                except Exception:
                    pass
            if (vc.is_playing() or vc.is_paused()) and vc.channel == ch:
                # Already streaming here (paused = VAD-idle, still live) —
                # on_ready refires on every gateway session resume; don't
                # tear down a healthy stream each time.
                return
            if vc.is_playing() or vc.is_paused():
                vc.stop()
                await asyncio.sleep(0.5)
            _play_paused(
                vc,
                _make_source(self.rpt.id, ch.guild.id, bot_obj=self),
                after=lambda e: self._after_play(e, vc),
            )
            log.info(f"Satellite '{self.rpt.id}' streaming → '{ch.name}' [{ch.guild.name}]")
        except NoAudioConfiguredError as exc:
            log.error(f"Satellite '{self.rpt.id}': {exc}")
        except Exception as exc:
            log.error(f"Satellite '{self.rpt.id}': join failed: {exc}")

    def _after_play(self, error: Optional[Exception], vc: discord.VoiceClient) -> None:
        """Playback-end hook — mirror the primary's auto-reconnect behavior."""
        if error:
            log.error(f"Satellite '{self.rpt.id}' player error: {error}")
        if self.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._auto_reconnect(vc), self.loop)

    async def _auto_reconnect(self, vc: discord.VoiceClient) -> None:
        await asyncio.sleep(2)
        if self.is_closed():
            return
        if vc.is_connected() and not vc.is_playing() and not vc.is_paused():
            try:
                _play_paused(
                    vc,
                    _make_source(self.rpt.id, vc.guild.id, bot_obj=self),
                    after=lambda e: self._after_play(e, vc),
                )
                log.info(f"Satellite '{self.rpt.id}' auto-reconnected [{vc.guild.name}]")
            except Exception as exc:
                log.error(f"Satellite '{self.rpt.id}' auto-reconnect failed: {exc}")
        elif not vc.is_connected():
            # Voice connection itself dropped — rejoin from scratch.
            await self._join_and_stream()

# ─────────────────────────────────────────────────────────────────────────────
# Access Control
# ─────────────────────────────────────────────────────────────────────────────

async def _has_access(ctx_or_ix) -> bool:
    if not cfg.bot.controller_role:
        return True
    member = getattr(ctx_or_ix, "author", None) or getattr(ctx_or_ix, "user", None)
    return member is not None and any(r.name == cfg.bot.controller_role for r in member.roles)

# ─────────────────────────────────────────────────────────────────────────────
# Embeds
# ─────────────────────────────────────────────────────────────────────────────

def _status_embed(guild_id: int) -> discord.Embed:
    gs         = get_state(guild_id)
    rpt        = cfg.repeater_by_id(gs.preset)
    rpt_name   = f"{rpt.display_name} ({rpt.frequency_mhz:.3f} MHz)" if rpt else gs.preset
    since      = f"<t:{int(gs.started_at)}:R>" if (gs.streaming and gs.started_at) else "Not streaming"
    color      = discord.Color.green() if gs.streaming else discord.Color.red()
    status_str = "🟢 Streaming" if gs.streaming else "🔴 Idle"

    e = discord.Embed(title=f"📻 {BOT_NAME}", color=color)
    e.add_field(name="Status",          value=status_str,                    inline=True)
    e.add_field(name="Repeater",        value=rpt_name,                      inline=True)
    e.add_field(name="Channel",         value=gs.channel,                    inline=True)
    e.add_field(name="Reconnects",      value=str(gs.reconnects),            inline=True)
    e.add_field(name="Streaming Since", value=since,                         inline=True)

    if rpt and rpt.sip_audio:
        e.add_field(
            name="Audio Source",
            value=f"SIP · `{rpt.sip_audio.host}:{rpt.sip_audio.port}` ext `{rpt.sip_audio.extension}`",
            inline=False,
        )
        state_text = _audio_state_text(guild_id)
        if state_text:
            e.add_field(name="SIP Connection", value=state_text, inline=True)
    else:
        e.add_field(name="Audio Source", value="⚠️ Not configured", inline=False)

    if cfg.tx.enabled:
        lock = _tx_locks.get(gs.preset)
        tx_text = f"🔴 **{lock.callsign}** keyed" if lock else "⚪ Idle"
        e.add_field(name="TX", value=tx_text, inline=True)

    # Unified per-repeater view: every repeater's always-on SIP monitor
    # state, plus its current role — 🔊 live in a voice channel (primary
    # preset or satellite), or 👂 monitor-only (still detecting, recording,
    # and posting activity full time; just not audible in Discord, which
    # for non-satellite repeaters needs a dedicated app token to change).
    rpt_lines = []
    for r in cfg.repeaters:
        client = _monitor_clients.get(r.id)
        if client:
            sip = _STATE_DISPLAY.get(client.state.name, client.state.name)
        elif not r.enabled:
            sip = "⏸️ Disabled"
        else:
            sip = "⚫ Not started"
        vc_r = _playback_vc_for(r.id)
        if vc_r is not None:
            role = f"🔊 Live in **{vc_r.channel.name}**"
        elif not r.enabled:
            role = "⏸️ Disabled in config"
        else:
            role = "👂 Monitoring (activity only)"
        sat_tag = " · 🛰 satellite" if r.discord.is_dedicated(cfg.bot.token) else ""
        mark = "▶" if r.id == gs.preset else "·"
        rpt_lines.append(
            f"{mark} `{r.id}` — {r.display_name} ({r.frequency_mhz:.3f} MHz)\n"
            f"    {sip} · {role}{sat_tag}"
        )
    e.add_field(name="Repeaters", value="\n".join(rpt_lines) or "—", inline=False)

    e.set_footer(text=f"{cfg.club.name} · {cfg.club.callsign} · {BOT_NAME}")
    return e


def _info_embed(guild_id: Optional[int] = None) -> discord.Embed:
    """
    Repeater info card: both repeaters, location, and live status.

    Status is always runtime truth, per repeater — _playback_vc_for() scans
    every guild, so this works with no guild context at all (e.g. /info in a
    DM). guild_id is accepted only to highlight the invoking guild's active
    preset. The old static config flag this used to fall back on could not
    track preset switches and is gone.
    """
    loc = cfg.club.location
    desc_parts = [f"[{loc.name}]({loc.maps_url})", loc.address]
    if cfg.club.website:
        desc_parts.append(cfg.club.website)
    if cfg.club.trustee:
        desc_parts.append(f"Trustee: {cfg.club.trustee}")

    e = discord.Embed(
        title       = f"📻 {cfg.club.callsign} — {cfg.club.name}",
        description = "\n".join(desc_parts),
        color       = discord.Color.dark_blue(),
    )

    # The invoking guild's active preset, used only for the ▶ highlight.
    active_preset: Optional[str] = None
    if guild_id is not None:
        gs = get_state(guild_id)
        if gs.streaming:
            active_preset = gs.preset

    for rpt in cfg.repeaters:
        offset_str = (
            f"+{rpt.offset_mhz:.3f} MHz" if rpt.offset_mhz > 0
            else f"{rpt.offset_mhz:.3f} MHz"
        )
        node_str  = rpt.allstar_node if rpt.allstar_node else "TBD"
        is_active = (active_preset == rpt.id)

        # Live, per-repeater status — every enabled repeater is monitored
        # full time, so "not the active preset" no longer means "inactive".
        vc_r = _playback_vc_for(rpt.id)
        if vc_r is not None:
            live_line = f"\n🔴 **Live on Discord** — #{vc_r.channel.name}"
        elif not rpt.enabled:
            live_line = "\n⏸️ Disabled in config"
        elif not rpt.sip_audio:
            live_line = "\n🔧 Audio source not yet configured"
        elif rpt.id in _monitor_clients:
            live_line = "\n👂 Monitoring — activity logged, not in a voice channel"
        else:
            live_line = "\n⚫ Monitor not started"

        value = (
            f"**Output:** {rpt.frequency_mhz:.3f} MHz\n"
            f"**Offset:** {offset_str}\n"
            f"**PL/CTCSS:** {rpt.pl_hz:.1f} Hz\n"
            f"**AllStar Node:** {node_str}"
            f"{live_line}"
        )

        indicator = "🟢" if (is_active or vc_r is not None) else "⚫"
        e.add_field(name=f"{indicator} {rpt.display_name} Repeater", value=value, inline=True)

    e.set_footer(text=f"{cfg.club.name} · {cfg.club.callsign} · {BOT_NAME}")
    return e

# ─────────────────────────────────────────────────────────────────────────────
# Control Panel View  (persistent across bot restarts)
# ─────────────────────────────────────────────────────────────────────────────

class ControlPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(
        self,
        ix: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        """
        Route view callback errors to our logger instead of stderr.

        discord.py's default View.on_error prints to sys.stderr, completely
        bypassing the logging system.  This override ensures every crash in a
        button handler appears in both the terminal and the log file with a
        full traceback, and attempts to acknowledge the interaction so Discord
        does not show 'This interaction failed' without any visible reason.
        """
        custom_id = getattr(item, "custom_id", repr(item))
        log.error(
            f"Unhandled exception in button '{custom_id}' "
            f"pressed by {ix.user} [{ix.guild}]: {error}",
            exc_info=error,
        )
        # Try to give the user some feedback
        try:
            if not ix.response.is_done():
                await ix.response.send_message(
                    f"❌ Unexpected error in button handler: `{error}`",
                    ephemeral=True,
                )
            else:
                await ix.followup.send(
                    f"❌ Unexpected error in button handler: `{error}`",
                    ephemeral=True,
                )
        except Exception:
            pass

    async def _deny(self, ix: discord.Interaction) -> bool:
        """Send a permission error and return True if access is denied."""
        if not await _has_access(ix):
            await ix.response.send_message("❌ You don't have permission.", ephemeral=True)
            return True
        return False

    async def _refresh(self, ix: discord.Interaction) -> None:
        """
        Update the panel embed after a deferred component interaction.

        After defer() on a button press discord.py uses type-6 (deferred_message_update).
        The correct follow-up call is edit_original_response() via the interaction
        webhook — NOT ix.message.edit() (direct channel API). Using the wrong endpoint
        is why Discord showed 'This interaction failed' even though the action itself
        completed successfully.
        """
        assert ix.guild is not None  # panel only exists in a guild
        try:
            await ix.edit_original_response(embed=_status_embed(ix.guild.id), view=self)
        except Exception as exc:
            log.warning(f"Panel refresh failed [{ix.guild.name}]: {exc}", exc_info=True)

    async def _error(self, ix: discord.Interaction, msg: str) -> None:
        """
        Send an ephemeral error message after the interaction has been deferred.
        Must use followup.send() rather than response.send_message() once deferred.
        """
        try:
            await ix.followup.send(msg, ephemeral=True)
        except Exception as exc:
            log.warning(f"Could not send panel error message: {exc}")

    # ── Row 1 ─────────────────────────────────────────────────────────────────

    @discord.ui.button(label="▶ Start",     style=discord.ButtonStyle.green,   custom_id="cp_start")
    async def btn_start(self, ix: discord.Interaction, _: discord.ui.Button):
        assert ix.guild is not None  # panel only exists in a guild
        log.debug(f"btn_start invoked by {ix.user} [{ix.guild}]")
        if await self._deny(ix): return
        # Use ix.member (always a Member in guild contexts) rather than ix.user
        # which can return a plain User with no .voice attribute.
        voice_channel = ix.member.voice.channel if (ix.member and ix.member.voice) else None
        if voice_channel is None:
            await ix.response.send_message("❌ Join a voice channel first.", ephemeral=True)
            return
        try:
            await ix.response.defer()
        except Exception as exc:
            log.error(f"btn_start: defer failed [{ix.guild.name}]: {exc}", exc_info=True)
            return
        try:
            msg = await _do_join(ix.guild, voice_channel)
            if msg.startswith("❌"):
                await self._error(ix, msg)
            else:
                log.info(f"Panel start: {msg}")
                await self._refresh(ix)
        except Exception as exc:
            log.error(f"btn_start failed [{ix.guild.name}]: {exc}", exc_info=True)
            await self._error(ix, f"❌ Failed to start stream: `{exc}`")

    @discord.ui.button(label="⏹ Stop",      style=discord.ButtonStyle.red,     custom_id="cp_stop")
    async def btn_stop(self, ix: discord.Interaction, _: discord.ui.Button):
        assert ix.guild is not None  # panel only exists in a guild
        if await self._deny(ix): return
        try:
            await ix.response.defer()
        except Exception as exc:
            log.error(f"btn_stop: defer failed [{ix.guild.name}]: {exc}", exc_info=True)
            return
        try:
            vc = ix.guild.voice_client
            if vc:
                vc.stop()
                await vc.disconnect()
            gs = get_state(ix.guild.id)
            gs.streaming  = False
            gs.started_at = None
            gs.channel    = "—"
            _clear_audio_client(ix.guild.id)
            log.info(f"Stream stopped via panel [{ix.guild.name}]")
            await self._refresh(ix)
        except Exception as exc:
            log.error(f"btn_stop failed [{ix.guild.name}]: {exc}", exc_info=True)
            await self._error(ix, f"❌ Failed to stop stream: `{exc}`")

    @discord.ui.button(label="🔄 Reconnect", style=discord.ButtonStyle.blurple, custom_id="cp_reconnect")
    async def btn_reconnect(self, ix: discord.Interaction, _: discord.ui.Button):
        assert ix.guild is not None  # panel only exists in a guild
        if await self._deny(ix): return
        vc = ix.guild.voice_client
        if vc is None:
            await ix.response.send_message("❌ Not connected.", ephemeral=True)
            return
        try:
            await ix.response.defer()
        except Exception as exc:
            log.error(f"btn_reconnect: defer failed [{ix.guild.name}]: {exc}", exc_info=True)
            return
        try:
            gs = get_state(ix.guild.id)
            vc.stop()
            await asyncio.sleep(1)
            _play_paused(vc, _make_source(gs.preset, ix.guild.id), after=lambda e, _vc=vc: _after_play(e, _vc))
            # User-requested reconnect — deliberately does NOT bump gs.reconnects.
            log.info(f"Manual reconnect via panel [{ix.guild.name}]")
            await self._refresh(ix)
        except Exception as exc:
            log.error(f"btn_reconnect failed [{ix.guild.name}]: {exc}", exc_info=True)
            await self._error(ix, f"❌ Reconnect failed: `{exc}`")


    async def _switch_via_panel(self, ix: discord.Interaction, preset_id: str) -> Optional[str]:
        """
        Panel-side preset switch. Returns an error/info string to show the
        user, or None on success. Mirrors _switch_to_preset's streaming
        branch: satellite repeaters redirect instead of switching, a
        VAD-paused stream counts as live, and a repeater with its own
        channel on the primary moves the bot there as part of the switch.
        """
        assert ix.guild is not None  # panel only exists in a guild
        rpt = cfg.repeater_by_id(preset_id)
        if rpt is None:
            return f"❌ Unknown repeater '{preset_id}'."
        if rpt.discord.is_dedicated(cfg.bot.token):
            sat_client = _monitor_clients.get(rpt.id)
            state = f" (SIP: {sat_client.state.name})" if sat_client else ""
            return (f"📡 **{rpt.display_name}** runs on its own bot in "
                    f"<#{rpt.discord.channel_id}>{state} — join that channel to listen.")
        gs = get_state(ix.guild.id)
        gs.preset = preset_id
        vc = ix.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await asyncio.sleep(0.5)
            if rpt.discord.channel_id and vc.channel.id != rpt.discord.channel_id:
                ch = bot.get_channel(rpt.discord.channel_id)
                if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                    await vc.move_to(ch)
                    gs.channel = ch.name
            _play_paused(vc, _make_source(gs.preset, ix.guild.id), after=lambda e, _vc=vc: _after_play(e, _vc))
            gs.started_at = datetime.now(timezone.utc).timestamp()   # switching preset is a fresh stream — reset "Stream Up"
            log.info(f"Switched to {preset_id} via panel [{ix.guild.name}]")
        return None

    # ── Row 2: repeater switchers ─────────────────────────────────────────────

    @discord.ui.button(label="📻 VHF 146.745", style=discord.ButtonStyle.blurple, custom_id="cp_vhf")
    async def btn_vhf(self, ix: discord.Interaction, _: discord.ui.Button):
        assert ix.guild is not None  # panel only exists in a guild
        if await self._deny(ix): return
        try:
            await ix.response.defer()
        except Exception as exc:
            log.error(f"btn_vhf: defer failed [{ix.guild.name}]: {exc}", exc_info=True)
            return
        try:
            note = await self._switch_via_panel(ix, "vhf")
            if note is not None:
                await ix.followup.send(note, ephemeral=True)
                return
            await self._refresh(ix)
        except Exception as exc:
            log.error(f"btn_vhf failed [{ix.guild.name}]: {exc}", exc_info=True)
            await self._error(ix, f"❌ Failed to switch to VHF: `{exc}`")

    @discord.ui.button(label="📡 UHF 448.775", style=discord.ButtonStyle.blurple, custom_id="cp_uhf")
    async def btn_uhf(self, ix: discord.Interaction, _: discord.ui.Button):
        assert ix.guild is not None  # panel only exists in a guild
        if await self._deny(ix): return
        uhf_rpt = cfg.repeater_by_id("uhf")
        if not uhf_rpt or not uhf_rpt.sip_audio:
            await ix.response.send_message(
                "⚫ UHF audio not yet configured. Add a `sip_audio:` block under `uhf:` in config.yaml.",
                ephemeral=True,
            )
            return
        try:
            await ix.response.defer()
        except Exception as exc:
            log.error(f"btn_uhf: defer failed [{ix.guild.name}]: {exc}", exc_info=True)
            return
        try:
            note = await self._switch_via_panel(ix, "uhf")
            if note is not None:
                await ix.followup.send(note, ephemeral=True)
                return
            await self._refresh(ix)
        except Exception as exc:
            log.error(f"btn_uhf failed [{ix.guild.name}]: {exc}", exc_info=True)
            await self._error(ix, f"❌ Failed to switch to UHF: `{exc}`")


# ─────────────────────────────────────────────────────────────────────────────
# Bot Setup
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(
    command_prefix=cfg.bot.prefix,
    intents=intents,
    help_command=None,        # disable built-in !help so we can register /help ourselves
)
_panel_view = ControlPanelView()


# ── Command invocation logging ────────────────────────────────────────────────
# before_invoke fires for both slash and prefix hybrid invocations.
# on_interaction fires for all slash/button/autocomplete interactions.
# Guard: skip before_invoke when ctx.interaction is set so that hybrid slash
# commands don't get logged twice (once here and once in on_interaction).

@bot.before_invoke
async def _log_prefix_cmd(ctx: commands.Context) -> None:
    if ctx.interaction is not None:
        return  # slash invocation — on_interaction handles logging
    args    = " ".join(f"{k}={v!r}" for k, v in ctx.kwargs.items())
    guild   = ctx.guild.name if ctx.guild else "DM"
    channel = getattr(ctx.channel, "name", "?")
    log.info(
        f"PREFIX  {ctx.prefix}{ctx.command.name}"
        f"{' ' + args if args else ''}  ·  "
        f"{ctx.author} ({ctx.author.id})  ·  #{channel} [{guild}]"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Events
# ─────────────────────────────────────────────────────────────────────────────

@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    """
    Log all interactions for auditability.

    In discord.py 2.x, slash commands are processed by CommandTree and button
    presses by the View store — both happen at the connection-state level
    BEFORE this event fires.  This handler is purely for logging; no explicit
    dispatch call is needed or available.
    """
    itype   = interaction.type
    user    = interaction.user
    guild   = interaction.guild.name if interaction.guild else "DM"
    channel = getattr(interaction.channel, "name", "?")

    if itype == discord.InteractionType.application_command:
        data     = interaction.data or {}
        cmd_name = data.get("name", "unknown")
        options  = {o["name"]: o.get("value", "…") for o in data.get("options", [])}
        args     = " ".join(f"{k}={v!r}" for k, v in options.items())
        log.info(
            f"SLASH  /{cmd_name}"
            f"{' ' + args if args else ''}  ·  "
            f"{user} ({user.id})  ·  #{channel} [{guild}]"
        )
    elif itype == discord.InteractionType.component:
        custom_id = (interaction.data or {}).get("custom_id", "?")
        log.info(f"BUTTON  {custom_id}  ·  {user} ({user.id})  ·  #{channel} [{guild}]")
    elif itype == discord.InteractionType.autocomplete:
        data     = interaction.data or {}
        cmd_name = data.get("name", "?")
        log.debug(f"AUTOCOMPLETE  /{cmd_name}  ·  {user}  ·  [{guild}]")

@bot.event
async def on_ready():
    global _loop
    _loop = asyncio.get_running_loop()

    bot.add_view(_panel_view)

    # Sync slash commands to the primary guild (instant).
    # Global sync is intentionally omitted: it takes up to 1 hour to propagate
    # and causes every command to appear twice (once guild-specific, once global)
    # for members of the primary guild.  If the bot is ever added to a second
    # server, re-enable global sync or add that guild's ID here.
    if cfg.bot.guild_id:
        g = discord.Object(id=cfg.bot.guild_id)
        bot.tree.copy_global_to(guild=g)
        await bot.tree.sync(guild=g)
        log.info(f"Slash commands synced to guild {cfg.bot.guild_id}.")
        # Purge stale GLOBAL registrations left over from any earlier run
        # that synced globally — those are what make every command appear
        # twice in the picker. Done via raw HTTP so the in-memory tree's
        # global set stays intact (clear_commands() on the tree would make
        # the next on_ready's copy_global_to() copy an empty set and wipe
        # the guild commands too).
        global _global_cmds_purged
        if not _global_cmds_purged:
            _global_cmds_purged = True
            try:
                await bot.http.bulk_upsert_global_commands(bot.application_id, [])
                log.info("Purged stale global slash-command registrations "
                         "(fixes duplicate entries in the command picker).")
            except Exception:
                log.warning("Global slash-command purge failed", exc_info=True)

    log.info(f"Bot ready: {bot.user} (ID: {bot.user.id})  ·  {len(bot.guilds)} guild(s)")
    log.info(f"Repeaters: {[r.id for r in cfg.repeaters]}")
    log.info(f"AMI: {'enabled' if cfg.asterisk.enabled else 'disabled'}  ·  "
             f"QRZ: {'configured' if _qrz else 'not configured'}  ·  "
             f"Activity channel: {cfg.activity.channel_id or 'none'}")

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{cfg.club.callsign} on AllStar"
        )
    )

    for guild in bot.guilds:
        get_state(guild.id)

    # Always-on SIP monitors for every repeater — independent of any voice
    # connection, so recording/activity posting runs full time from boot.
    _ensure_monitors()

    # Auto-join the default preset's effective channel. With no per-repeater
    # discord: blocks this inherits bot.auto_join_channel_id — identical to
    # the old behavior; with per-repeater channels configured, the primary
    # starts in the channel belonging to the repeater it will stream.
    _default_rpt = cfg.repeater_by_id(cfg.default_preset)
    _auto_ch_id  = (
        (_default_rpt.discord.channel_id if _default_rpt else 0)
        or cfg.bot.auto_join_channel_id
    )
    if _auto_ch_id:
        ch = bot.get_channel(_auto_ch_id)
        if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            result = await _do_join(ch.guild, ch)
            log.info(f"Auto-join: {result}")
        else:
            log.warning(
                f"auto-join channel {_auto_ch_id} "
                "is not a valid voice/stage channel."
            )

    if not watchdog.is_running():
        watchdog.start()
    if not state_sync.is_running():
        state_sync.start()
    if cfg.asterisk.enabled and cfg.has_activity_channels():
        if not activity_feed.is_running():
            activity_feed.start()
            log.info("Node activity monitor started.")
    if cfg.has_activity_channels():
        if not sip_health_watch.is_running():
            sip_health_watch.start()
            log.info("SIP health watch started.")
    if cfg.tx.enabled:
        if not tx_lock_watch.is_running():
            tx_lock_watch.start()
            log.info("TX lock watch started.")
        asyncio.create_task(_verify_tx_operators_via_qrz())


@bot.event
async def on_guild_join(guild: discord.Guild):
    get_state(guild.id)
    log.info(f"Joined new guild: {guild.name} (ID: {guild.id})")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.CommandNotFound, commands.MissingRequiredArgument)):
        return
    if isinstance(error, commands.NoPrivateMessage):
        try:
            await ctx.send("❌ This command only works in a server, not in DMs.", ephemeral=True)
        except Exception:
            pass
        return
    log.error(f"Command error [{ctx.command}]: {error}")
    try:
        await ctx.send(f"❌ Something went wrong running that command: `{error}`", ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after:  discord.VoiceState,
) -> None:
    """
    Detect when the bot leaves a voice channel.

    discord.py fires this with after.channel=None during both:
      (a) true disconnects (admin kick, /leave), and
      (b) transient WebSocket 1001 reconnects — where the voice socket drops
          briefly but discord.py internally reconnects and resumes audio playback.

    We distinguish them by waiting one second and then checking whether the
    bot's voice_client is still connected.  If it is, this was a reconnect
    and we leave state alone.  If it isn't, it was a true disconnect.
    """
    if member != bot.user:
        return
    if not (before.channel and after.channel is None):
        return

    channel_name = before.channel.name
    guild        = member.guild

    # Brief wait lets discord.py complete its internal reconnect if this is
    # a transient drop rather than a real disconnect.
    await asyncio.sleep(1.5)

    vc = guild.voice_client
    if vc and vc.is_connected():
        # Transient reconnect — discord.py already recovered; leave state alone
        log.debug(
            f"Voice socket briefly dropped for '{channel_name}' [{guild.name}] "
            f"but reconnected automatically — state preserved."
        )
        return

    # True disconnect (admin kick, network loss with no recovery, /leave)
    gs = get_state(guild.id)
    gs.streaming  = False
    gs.started_at = None
    gs.channel    = "—"
    _clear_audio_client(guild.id)
    log.warning(f"Bot truly disconnected from '{channel_name}' [{guild.name}]")

# ─────────────────────────────────────────────────────────────────────────────
# Core Join Logic
# ─────────────────────────────────────────────────────────────────────────────

async def _do_join(guild: discord.Guild, channel) -> str:
    gs = get_state(guild.id)
    vc = guild.voice_client

    # Short-circuit before doing anything if already streaming in this channel
    if vc and (vc.is_playing() or vc.is_paused()) and vc.channel == channel:
        return f"✅ Already streaming in **{channel.name}**."

    # TX: connect with a voice-receive-capable client class if TX is enabled
    # and the optional dependency is available. Falls back to a plain
    # discord.VoiceClient (TX simply unavailable) otherwise — see
    # _get_voice_recv_client_cls().
    voice_recv_cls = _get_voice_recv_client_cls() if cfg.tx.enabled else None

    try:
        if vc is None:
            connect_kwargs = {"timeout": 15, "reconnect": True}
            if voice_recv_cls is not None:
                connect_kwargs["cls"] = voice_recv_cls
            vc = await channel.connect(**connect_kwargs)
        elif vc.channel != channel:
            await vc.move_to(channel)
    except Exception as exc:
        return f"❌ Connection failed: `{exc}`"

    # Stage Channel: request speaker status automatically
    if isinstance(channel, discord.StageChannel):
        try:
            await guild.me.edit(suppress=False)
        except Exception:
            pass

    # TX: attach the receive sink if this connection actually supports it.
    # (If the bot was already connected via a plain VoiceClient from before
    # TX was enabled, this won't upgrade it — a fresh /join or /leave+/join
    # picks up voice_recv_cls next time.)
    if voice_recv_cls is not None and isinstance(vc, voice_recv_cls):
        try:
            vc.listen(_make_tx_sink(guild.id), after=_make_tx_listen_after(guild.id))
        except Exception as exc:
            log.warning(f"TX: failed to attach voice-receive sink [{guild.name}]: {exc}")

    if vc.is_playing() or vc.is_paused():
        vc.stop()
        await asyncio.sleep(0.5)

    try:
        _play_paused(vc, _make_source(gs.preset, guild.id), after=lambda e: _after_play(e, vc))
    except NoAudioConfiguredError as exc:
        # No point sitting silently in a voice channel with nothing to play —
        # leave cleanly rather than leaving the user to notice and /leave manually.
        try:
            await vc.disconnect()
        except Exception:
            pass
        return f"❌ {exc}"

    gs.streaming  = True
    gs.started_at = datetime.now(timezone.utc).timestamp()
    gs.channel    = channel.name
    ts = int(gs.started_at)

    rpt   = cfg.repeater_by_id(gs.preset)
    label = rpt.display_name if rpt else gs.preset

    log.info(f"Streaming → '{channel.name}' [{gs.preset}] in '{guild.name}'")
    return f"📻 Streaming **{label}** → **{channel.name}** (started <t:{ts}:R>)"

# ─────────────────────────────────────────────────────────────────────────────
# Commands  (hybrid = /slash  AND  !prefix)
# ─────────────────────────────────────────────────────────────────────────────

@bot.hybrid_command(name="join", description="Join your voice channel and start streaming.")
@commands.guild_only()
async def join_cmd(ctx: commands.Context):
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    if not await _has_access(ctx):
        return await ctx.send("❌ No permission.", ephemeral=True)
    if ctx.author.voice is None:
        return await ctx.send("❌ Join a voice channel first.", ephemeral=True)
    await ctx.defer()
    await ctx.send(await _do_join(ctx.guild, ctx.author.voice.channel))


@bot.hybrid_command(name="leave", description="Stop streaming and disconnect.")
@commands.guild_only()
async def leave_cmd(ctx: commands.Context):
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    if not await _has_access(ctx):
        return await ctx.send("❌ No permission.", ephemeral=True)
    vc = ctx.voice_client
    if vc is None:
        return await ctx.send("❌ Not in a voice channel.", ephemeral=True)
    name = vc.channel.name
    vc.stop()
    await vc.disconnect()
    gs = get_state(ctx.guild.id)
    gs.streaming  = False
    gs.started_at = None
    gs.channel    = "—"
    _clear_audio_client(ctx.guild.id)
    log.info(f"Left '{name}' [{ctx.guild.name}]")
    await ctx.send(f"📴 Left **{name}**.")


@bot.hybrid_command(name="status", description="Show current stream status.")
@commands.guild_only()
async def status_cmd(ctx: commands.Context):
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    await ctx.send(embed=_status_embed(ctx.guild.id), ephemeral=True)


@bot.hybrid_command(name="panel", description="Post the interactive control panel here.")
@commands.guild_only()
async def panel_cmd(ctx: commands.Context):
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    await ctx.send(embed=_status_embed(ctx.guild.id), view=_panel_view)


@bot.hybrid_command(name="info", description="Show K2BR repeater information.")
async def info_cmd(ctx: commands.Context):
    """Repeater info card: both machines, frequencies, PL tones, location."""
    # Works in DMs too — _info_embed already supports guild_id=None, falling
    # back to config.yaml's static streaming_now flag instead of live guild
    # state, since there's no guild-specific session to report from a DM.
    guild_id = ctx.guild.id if ctx.guild else None
    await ctx.send(embed=_info_embed(guild_id))


async def _switch_to_preset(ctx: commands.Context, preset_id: str) -> None:
    """
    Shared logic for /vhf, /uhf, and /stream.
    - If the bot is playing in this guild: swap the stream immediately.
    - If the user is in a voice channel but bot isn't: join and start.
    - Otherwise: set the preset so the next /join or ▶ Start uses it.
    """
    assert ctx.guild is not None  # guaranteed by the @commands.guild_only() callers
    if not await _has_access(ctx):
        await ctx.send("❌ No permission.", ephemeral=True)
        return
    rpt = cfg.repeater_by_id(preset_id)
    if rpt is None:
        opts = ", ".join(f"`{r.id}`" for r in cfg.repeaters)
        await ctx.send(f"❌ Unknown repeater. Available: {opts}", ephemeral=True)
        return
    if not rpt.sip_audio:
        await ctx.send(
            f"⚫ The **{preset_id}** repeater has no audio source configured yet. "
            f"Add a `sip_audio:` block under `{preset_id}:` in config.yaml.",
            ephemeral=True,
        )
        return

    # Repeaters with a dedicated app token stream on their own satellite bot
    # in their own channel — the primary can't (and needn't) "switch" to
    # them. Point the user at the right channel instead.
    if rpt.discord.is_dedicated(cfg.bot.token):
        sat_client = _monitor_clients.get(rpt.id)
        state = f" (SIP: {sat_client.state.name})" if sat_client else ""
        await ctx.send(
            f"📡 **{rpt.display_name}** runs on its own bot in <#{rpt.discord.channel_id}>"
            f"{state} — join that channel to listen.",
            ephemeral=True,
        )
        return

    gs        = get_state(ctx.guild.id)
    gs.preset = preset_id
    vc        = ctx.voice_client
    label     = rpt.display_name
    freq      = f" ({rpt.frequency_mhz:.3f} MHz)"

    # Channel-aware switch: if this repeater declares its own voice channel
    # and the bot is currently parked elsewhere, move as part of the switch.
    # Same channel id on every repeater (the inherited default) makes this a
    # no-op — i.e. exactly the old in-place switch behavior.
    target_ch = None
    if vc and rpt.discord.channel_id and vc.channel.id != rpt.discord.channel_id:
        maybe = bot.get_channel(rpt.discord.channel_id)
        if isinstance(maybe, (discord.VoiceChannel, discord.StageChannel)):
            target_ch = maybe

    if vc and (vc.is_playing() or vc.is_paused()):
        await ctx.defer()
        vc.stop()
        await asyncio.sleep(0.5)
        if target_ch is not None:
            await vc.move_to(target_ch)
            gs.channel = target_ch.name
        _play_paused(vc, _make_source(gs.preset, ctx.guild.id), after=lambda e: _after_play(e, vc))
        gs.started_at = datetime.now(timezone.utc).timestamp()   # switching preset is a fresh stream — reset "Stream Up"
        moved = f" in **{target_ch.name}**" if target_ch is not None else ""
        log.info(f"Switched to preset '{preset_id}'{' + moved channel' if target_ch else ''} [{ctx.guild.name}]")
        await ctx.send(f"🔀 Now streaming **{label}**{freq}{moved}")

    elif ctx.author.voice:
        await ctx.defer()
        result = await _do_join(ctx.guild, ctx.author.voice.channel)
        await ctx.send(result)

    else:
        log.info(f"Preset queued → '{preset_id}' (bot idle) [{ctx.guild.name}]")
        await ctx.send(
            f"✅ Preset set to **{label}**{freq}. "
            f"Join a voice channel and use `/join` (or ▶ Start) to begin streaming."
        )


@bot.hybrid_command(name="vhf", description="Switch to (or start) the VHF repeater — 146.745 MHz.")
@commands.guild_only()
async def vhf_cmd(ctx: commands.Context):
    await _switch_to_preset(ctx, "vhf")


@bot.hybrid_command(name="uhf", description="Switch to (or start) the UHF repeater — 448.775 MHz.")
@commands.guild_only()
async def uhf_cmd(ctx: commands.Context):
    await _switch_to_preset(ctx, "uhf")


@bot.hybrid_command(name="stream", description="Switch to a named stream preset.")
@commands.guild_only()
@app_commands.describe(preset="Preset name — type to see options")
async def stream_cmd(ctx: commands.Context, preset: str):
    await _switch_to_preset(ctx, preset)

@stream_cmd.autocomplete("preset")
async def _preset_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=f"{r.display_name} ({r.id})", value=r.id)
        for r in cfg.repeaters if current.lower() in r.id.lower()
    ]


@bot.hybrid_command(name="reconnect", description="Force-reconnect the audio stream.")
@commands.guild_only()
async def reconnect_cmd(ctx: commands.Context):
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    if not await _has_access(ctx):
        return await ctx.send("❌ No permission.", ephemeral=True)
    vc = ctx.voice_client
    if vc is None:
        return await ctx.send("❌ Not connected. Use `/join` first.", ephemeral=True)
    await ctx.defer()
    gs = get_state(ctx.guild.id)
    vc.stop()
    await asyncio.sleep(1)
    try:
        _play_paused(vc, _make_source(gs.preset, ctx.guild.id), after=lambda e: _after_play(e, vc))
    except NoAudioConfiguredError as exc:
        return await ctx.send(f"❌ {exc}")
    # User-requested reconnect — deliberately does NOT bump gs.reconnects.
    log.info(f"Manual reconnect [{ctx.guild.name}]")
    await ctx.send("🔄 Stream reconnected.")


@bot.hybrid_command(name="presets", description="List all configured repeaters.")
@commands.guild_only()
async def presets_cmd(ctx: commands.Context):
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    gs = get_state(ctx.guild.id)
    e  = discord.Embed(title="📋 Repeaters", color=discord.Color.blurple())
    for rpt in cfg.repeaters:
        marker = " ✅ active" if rpt.id == gs.preset else ""
        audio  = f"`{rpt.sip_audio.host}:{rpt.sip_audio.port}`" if rpt.sip_audio else "⚠️ not configured"
        e.add_field(
            name=f"`{rpt.id}`{marker}",
            value=f"{rpt.display_name} ({rpt.frequency_mhz:.3f} MHz)\n{audio}",
            inline=False,
        )
    await ctx.send(embed=e, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
# Repeater Control Commands  (require operator_role or controller_role)
# ─────────────────────────────────────────────────────────────────────────────

async def _has_ami_access(ctx_or_ix) -> bool:
    """Check for the AMI operator role (falls back to controller_role)."""
    role = cfg.asterisk.operator_role or cfg.bot.controller_role
    if not role:
        return True
    member = getattr(ctx_or_ix, "author", None) or getattr(ctx_or_ix, "user", None)
    return member is not None and any(r.name == role for r in member.roles)


def _ami_for(rpt):
    """AMIClient for a specific repeater, or None if it has no AMI/node."""
    if rpt is None or rpt.ami is None or not rpt.allstar_node:
        return None
    return AMIClient(rpt.ami.host, rpt.ami.port, rpt.ami.username, rpt.ami.password)


def _active_ami(guild_id: int):
    """Return (RepeaterConfig, AMIClient) for the guild's active preset, or (None, None)."""
    rpt = cfg.repeater_by_id(get_state(guild_id).preset)
    client = _ami_for(rpt)
    return (rpt, client) if client is not None else (None, None)


def _resolve_target_repeater(ctx: commands.Context, explicit: Optional[str]):
    """
    Decide which repeater an operator command targets. Returns
    (RepeaterConfig | None, provenance_str). Resolution order:

      1. Explicit `repeater` argument — always wins. Unknown id → (None,
         error text) so the caller can show the valid options.
      2. Channel inference — if the invoking channel is uniquely one
         repeater's voice channel or activity channel, target that
         repeater. With per-repeater channels configured, running
         /repeater-cmd in #uhf-activity naturally targets UHF. Shared
         channels (the classic combined setup, or today's config with the
         same ids on both) match multiple repeaters and fall through —
         this tier only acts when it's unambiguous.
      3. The guild's active preset — the pre-7.x behavior, still the
         fallback so nothing changes until channels are split.

    Provenance is a short human string ("requested", "this channel",
    "active preset") that callers should surface in their reply, so the
    operator always sees WHICH repeater was acted on and why.
    """
    if explicit:
        rpt = cfg.repeater_by_id(explicit.strip().lower())
        if rpt is None:
            opts = ", ".join(f"`{r.id}`" for r in cfg.repeaters)
            return None, f"Unknown repeater `{explicit}` — configured: {opts}"
        return rpt, "requested"

    # Check the invoking channel AND its parent (threads under a repeater's
    # activity channel should infer that repeater — replying in a thread on
    # an activity post is a natural place to run these commands).
    ch        = ctx.channel
    ch_ids    = set()
    if ch is not None:
        ch_ids.add(ch.id)
        parent_id = getattr(ch, "parent_id", None)
        if parent_id:
            ch_ids.add(parent_id)
    if ch_ids:
        matches = [
            r for r in cfg.repeaters
            if r.discord and ch_ids & {r.discord.channel_id, r.discord.activity_channel_id}
        ]
        if len(matches) == 1:
            return matches[0], "this channel"

    assert ctx.guild is not None  # only reached from guild-only operator commands
    rpt = cfg.repeater_by_id(get_state(ctx.guild.id).preset)
    return rpt, "active preset"


def _target_note(rpt, how: str) -> str:
    """
    Human-readable provenance suffix for command replies, always naming the
    resolved repeater: "*(target: UHF · this channel)*". Appended to every
    targeted command's reply so the operator can see which repeater was
    acted on AND why it was chosen, at a glance.
    """
    return f"*(target: {rpt.display_name} · {how})*"


async def _ami_check(ctx: commands.Context) -> bool:
    """Gate: check access and AMI enabled. Sends an error and returns False if not ok."""
    if not await _has_ami_access(ctx):
        await ctx.send("❌ You need the operator role to use repeater control commands.", ephemeral=True)
        return False
    if not cfg.asterisk.enabled:
        await ctx.send("❌ Asterisk control is not enabled in config.yaml.", ephemeral=True)
        return False
    return True


@bot.hybrid_command(name="link-repeaters", description="Link the VHF and UHF repeaters together.")
@commands.guild_only()
async def link_repeaters_cmd(ctx: commands.Context):
    """Links VHF node 50420 ↔ UHF node 53209 via the VHF AMI."""
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    if not await _ami_check(ctx): return
    vhf = cfg.repeater_by_id("vhf")
    uhf = cfg.repeater_by_id("uhf")
    if not vhf or not uhf or not vhf.ami or not vhf.allstar_node or not uhf.allstar_node:
        return await ctx.send(
            "❌ Both repeaters must have `allstar_node` and `ami` configured in config.yaml.",
            ephemeral=True,
        )
    await ctx.defer()
    try:
        client = AMIClient(vhf.ami.host, vhf.ami.port, vhf.ami.username, vhf.ami.password)
        # ilink 13 = permanently connect specified link, transceive. Matches
        # this club's own [functions53209] DTMF entry 83 for the same
        # VHF<->UHF bridge ("cmd,...rpt cmd 53209 ilink 13 50420") — using
        # "permanent" here, not the plain ilink 3, mirrors their own config's
        # intent that this is a standing link, not a transient session one.
        await client.ilink(vhf.allstar_node, f"13 {uhf.allstar_node}")
        log.info(f"Link repeaters: {vhf.allstar_node} → {uhf.allstar_node} [{ctx.guild.name}]")
        await ctx.send(
            f"🔗 Linking **{vhf.display_name}** (Node `{vhf.allstar_node}`) "
            f"↔ **{uhf.display_name}** (Node `{uhf.allstar_node}`)"
        )
    except Exception as exc:
        log.error(f"link-repeaters failed: {exc}")
        await ctx.send(f"❌ AMI error: `{exc}`")


@bot.hybrid_command(name="unlink-repeaters", description="Unlink the VHF and UHF repeaters.")
@commands.guild_only()
async def unlink_repeaters_cmd(ctx: commands.Context):
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    if not await _ami_check(ctx): return
    vhf = cfg.repeater_by_id("vhf")
    uhf = cfg.repeater_by_id("uhf")
    if not vhf or not uhf or not vhf.ami or not vhf.allstar_node or not uhf.allstar_node:
        return await ctx.send(
            "❌ Both repeaters must have `allstar_node` and `ami` configured in config.yaml.",
            ephemeral=True,
        )
    await ctx.defer()
    try:
        client = AMIClient(vhf.ami.host, vhf.ami.port, vhf.ami.username, vhf.ami.password)
        # ilink 11 = disconnect a previously permanently connected link — the
        # counterpart to ilink 13 above. Matches [functions53209] entry 84
        # ("cmd,...rpt cmd 53209 ilink 11 50420").
        await client.ilink(vhf.allstar_node, f"11 {uhf.allstar_node}")
        log.info(f"Unlink repeaters: {vhf.allstar_node} ✗ {uhf.allstar_node} [{ctx.guild.name}]")
        await ctx.send(
            f"🔌 Unlinked **{vhf.display_name}** (Node `{vhf.allstar_node}`) "
            f"from **{uhf.display_name}** (Node `{uhf.allstar_node}`)"
        )
    except Exception as exc:
        log.error(f"unlink-repeaters failed: {exc}")
        await ctx.send(f"❌ AMI error: `{exc}`")


@bot.hybrid_command(name="link", description="Link a repeater to an AllStar node.")
@commands.guild_only()
@app_commands.describe(
    node="Remote AllStar node number to link to",
    repeater="Which repeater (default: this channel's repeater, else the active preset)",
)
async def link_cmd(ctx: commands.Context, node: str, repeater: Optional[str] = None):
    if not await _ami_check(ctx): return
    if not node.isdigit():
        return await ctx.send("❌ Node number must be digits only (e.g. `27339`).", ephemeral=True)
    rpt, how = _resolve_target_repeater(ctx, repeater)
    if rpt is None:
        return await ctx.send(f"❌ {how}", ephemeral=True)
    client = _ami_for(rpt)
    if client is None:
        return await ctx.send(f"❌ **{rpt.display_name}** has no AMI configured.", ephemeral=True)
    await ctx.defer()
    try:
        # ilink 3 = connect specified link, transceive (non-permanent —
        # for a standing/permanent link use ilink 13 instead, as
        # link_repeaters_cmd does for the VHF<->UHF bridge).
        await client.ilink(rpt.allstar_node, f"3 {node}")
        log.info(f"Link: {rpt.allstar_node} → {node} [{rpt.id} · {how}]")
        await ctx.send(
            f"🔗 Linking **{rpt.display_name}** (Node `{rpt.allstar_node}`) → Node `{node}` "
            f"{_target_note(rpt, how)}"
        )
    except Exception as exc:
        log.error(f"link failed: {exc}")
        await ctx.send(f"❌ AMI error: `{exc}`")


@bot.hybrid_command(name="unlink", description="Unlink a repeater from a specific node.")
@commands.guild_only()
@app_commands.describe(
    node="AllStar node number to disconnect",
    repeater="Which repeater (default: this channel's repeater, else the active preset)",
)
async def unlink_cmd(ctx: commands.Context, node: str, repeater: Optional[str] = None):
    if not await _ami_check(ctx): return
    if not node.isdigit():
        return await ctx.send("❌ Node number must be digits only.", ephemeral=True)
    rpt, how = _resolve_target_repeater(ctx, repeater)
    if rpt is None:
        return await ctx.send(f"❌ {how}", ephemeral=True)
    client = _ami_for(rpt)
    if client is None:
        return await ctx.send(f"❌ **{rpt.display_name}** has no AMI configured.", ephemeral=True)
    await ctx.defer()
    try:
        await client.ilink(rpt.allstar_node, f"1 {node}")   # disconnect specified link
        log.info(f"Unlink: {rpt.allstar_node} ✗ {node} [{rpt.id} · {how}]")
        await ctx.send(
            f"🔌 Unlinking **{rpt.display_name}** (Node `{rpt.allstar_node}`) from Node `{node}` "
            f"{_target_note(rpt, how)}"
        )
    except Exception as exc:
        log.error(f"unlink failed: {exc}")
        await ctx.send(f"❌ AMI error: `{exc}`")


@bot.hybrid_command(name="unlink-all", description="Disconnect ALL links on a repeater.")
@commands.guild_only()
@app_commands.describe(repeater="Which repeater (default: this channel's repeater, else the active preset)")
async def unlink_all_cmd(ctx: commands.Context, repeater: Optional[str] = None):
    if not await _ami_check(ctx): return
    rpt, how = _resolve_target_repeater(ctx, repeater)
    if rpt is None:
        return await ctx.send(f"❌ {how}", ephemeral=True)
    client = _ami_for(rpt)
    if client is None:
        return await ctx.send(f"❌ **{rpt.display_name}** has no AMI configured.", ephemeral=True)
    await ctx.defer()
    try:
        await client.ilink(rpt.allstar_node, "6")   # disconnect all links
        log.info(f"Unlink-all: {rpt.allstar_node} [{rpt.id} · {how}]")
        await ctx.send(
            f"🔌 Disconnected all links on **{rpt.display_name}** (Node `{rpt.allstar_node}`) "
            f"{_target_note(rpt, how)}"
        )
    except Exception as exc:
        log.error(f"unlink-all failed: {exc}")
        await ctx.send(f"❌ AMI error: `{exc}`")


@bot.hybrid_command(name="monitor-node", description="Connect to a node in listen-only (monitor) mode.")
@commands.guild_only()
@app_commands.describe(
    node="AllStar node number to monitor",
    repeater="Which repeater (default: this channel's repeater, else the active preset)",
)
async def monitor_node_cmd(ctx: commands.Context, node: str, repeater: Optional[str] = None):
    if not await _ami_check(ctx): return
    if not node.isdigit():
        return await ctx.send("❌ Node number must be digits only.", ephemeral=True)
    rpt, how = _resolve_target_repeater(ctx, repeater)
    if rpt is None:
        return await ctx.send(f"❌ {how}", ephemeral=True)
    client = _ami_for(rpt)
    if client is None:
        return await ctx.send(f"❌ **{rpt.display_name}** has no AMI configured.", ephemeral=True)
    await ctx.defer()
    try:
        await client.ilink(rpt.allstar_node, f"2 {node}")   # connect specified link, monitor only
        log.info(f"Monitor: {rpt.allstar_node} → {node} (listen-only) [{rpt.id} · {how}]")
        await ctx.send(
            f"👂 Monitoring Node `{node}` via **{rpt.display_name}** (Node `{rpt.allstar_node}`) "
            f"— listen-only {_target_note(rpt, how)}"
        )
    except Exception as exc:
        log.error(f"monitor-node failed: {exc}")
        await ctx.send(f"❌ AMI error: `{exc}`")


@bot.hybrid_command(name="node-status", description="Show nodes currently linked to the repeaters.")
async def node_status_cmd(ctx: commands.Context):
    if not cfg.asterisk.enabled:
        return await ctx.send("❌ Asterisk control is not enabled.", ephemeral=True)
    if not await _has_ami_access(ctx):
        return await ctx.send("❌ You need the operator role to view node status.", ephemeral=True)
    await ctx.defer()
    e = discord.Embed(title="📡 Node Status", color=discord.Color.blurple())

    for rpt in cfg.repeaters:
        if not rpt.allstar_node or not rpt.ami:
            e.add_field(
                name=f"{rpt.display_name} ({rpt.frequency_mhz:.3f} MHz)",
                value="⚙️ No AMI configured",
                inline=False,
            )
            continue
        try:
            client   = AMIClient(rpt.ami.host, rpt.ami.port, rpt.ami.username, rpt.ami.password)
            # rpt nodes: clean comma-separated list with T/R prefix
            raw_list = await client.get_node_list_raw(rpt.allstar_node)
            # rpt stats: extract uptime and autopatch state
            stats    = await client.get_status_text(rpt.allstar_node)
            age      = node_monitor.poll_age(rpt.id)
            age_str  = f"  ·  activity feed polled {int(age)}s ago" if age is not None else ""

            # Pull key lines from rpt stats
            uptime_line   = ""
            autopatch_line= ""
            for line in stats.splitlines():
                if "Uptime" in line and ":" in line:
                    uptime_line = line.split(":", 1)[1].strip()
                if "Autopatch state" in line and ":" in line:
                    autopatch_line = line.split(":", 1)[1].strip()

            value_parts = [f"🔗 **Linked:** `{raw_list}`"]
            if uptime_line:
                value_parts.append(f"⏱ **Uptime:** {uptime_line}")
            if autopatch_line:
                value_parts.append(f"📞 **Autopatch:** {autopatch_line}")
            value_parts.append(f"[T=transceive · R=receive-only]{age_str}")

        except Exception as exc:
            value_parts = [f"❌ AMI unreachable: `{exc}`"]

        e.add_field(
            name=f"{rpt.display_name} — Node `{rpt.allstar_node}` ({rpt.frequency_mhz:.3f} MHz)",
            value="\n".join(value_parts),
            inline=False,
        )

    e.set_footer(text=f"{cfg.club.callsign} · {BOT_NAME}")
    await ctx.send(embed=e)


@bot.hybrid_command(name="tx-status", description="Show who (if anyone) currently holds TX on each repeater.")
async def tx_status_cmd(ctx: commands.Context):
    if not cfg.tx.enabled:
        return await ctx.send("❌ TX is not enabled in config.yaml.", ephemeral=True)
    e = discord.Embed(title="🎙️ TX Status", color=discord.Color.blurple())
    now = time.time()
    for rpt in cfg.repeaters:
        lock = _tx_locks.get(rpt.id)
        if lock is None:
            value = "⚪ Idle"
        else:
            held_for = int(now - lock.started_at)
            value = f"🔴 **{lock.callsign}** — keyed {held_for}s (max {cfg.tx.max_transmission_seconds}s)"
        e.add_field(name=f"{rpt.display_name} ({rpt.frequency_mhz:.3f} MHz)", value=value, inline=False)
    e.set_footer(text=f"{len(cfg.tx_operators)} authorized TX operator(s) configured")
    await ctx.send(embed=e, ephemeral=True)


@bot.hybrid_command(name="tx-kill", description="Immediately force-release the TX lock on a repeater.")
@commands.guild_only()
@app_commands.describe(repeater="Which repeater (default: this channel's repeater, else the active preset)")
async def tx_kill_cmd(ctx: commands.Context, repeater: Optional[str] = None):
    if not await _has_ami_access(ctx):
        return await ctx.send("❌ You need the operator role to do this.", ephemeral=True)
    assert ctx.guild is not None  # guaranteed by @commands.guild_only()
    if not cfg.tx.enabled:
        return await ctx.send("❌ TX is not enabled in config.yaml.", ephemeral=True)
    rpt, how = _resolve_target_repeater(ctx, repeater)
    if rpt is None:
        return await ctx.send(f"❌ {how}", ephemeral=True)
    lock = _tx_locks.pop(rpt.id, None)
    if lock is None:
        return await ctx.send(
            f"✅ **{rpt.display_name}** isn't currently keyed by anyone. {_target_note(rpt, how)}"
        )
    _tx_release_ptt(rpt.id)
    log.warning(
        f"TX force-killed on {rpt.id} (was held by {lock.callsign} / "
        f"{lock.holder_user_id}) [{ctx.guild.name}]"
    )
    await _post_tx_event(rpt.id, lock.callsign, "timeout")
    await ctx.send(
        f"🛑 Force-released TX on **{rpt.display_name}** (was: **{lock.callsign}**) "
        f"{_target_note(rpt, how)}"
    )


@bot.hybrid_command(
    name="repeater-cmd",
    description="Run a named HamVOIP action (e.g. time announcement) on a repeater.",
)
@commands.guild_only()
@app_commands.describe(
    command="Which action to run — type to see options",
    repeater="Which repeater (default: this channel's repeater, else the active preset)",
)
async def repeater_cmd_cmd(ctx: commands.Context, command: str, repeater: Optional[str] = None):
    if not await _ami_check(ctx): return
    entry = cfg.repeater_command_by_id(command)
    if entry is None:
        opts = ", ".join(f"`{c.id}`" for c in cfg.repeater_commands)
        return await ctx.send(f"❌ Unknown command. Available: {opts or '(none configured)'}", ephemeral=True)

    rpt, how = _resolve_target_repeater(ctx, repeater)
    if rpt is None:
        return await ctx.send(f"❌ {how}", ephemeral=True)
    client = _ami_for(rpt)
    if client is None:
        return await ctx.send(f"❌ **{rpt.display_name}** has no AMI configured.", ephemeral=True)

    if not entry.valid_for(rpt.id):
        scoped_to = ", ".join(f"`{r}`" for r in entry.repeaters)
        return await ctx.send(
            f"❌ **{entry.label}** is only configured for {scoped_to} — "
            f"this would target **{rpt.display_name}** (`{rpt.id}`, via {how}). "
            f"Re-run with the `repeater` "
            f"option set to one of {scoped_to}, or add `{rpt.id}` to this "
            f"command's `repeaters:` list in config.yaml if you've "
            f"confirmed it works there too.",
            ephemeral=True,
        )
    if entry.command == REPEATER_COMMAND_PLACEHOLDER:
        return await ctx.send(
            f"❌ **{entry.label}** isn't configured yet — its `command:` in config.yaml "
            f"is still the `{REPEATER_COMMAND_PLACEHOLDER}` placeholder. "
            f"Fill in the real Asterisk command for your node first.",
            ephemeral=True,
        )

    await ctx.defer()
    ami_command = entry.command.format(node=rpt.allstar_node)
    # Log the concrete destination up front: which repeater, which AllStar node,
    # and which AMI endpoint the command is dispatched to. (The invoking channel
    # is already logged on the SLASH line above; the resolution provenance is in
    # the Discord reply. What matters here is where it actually went.)
    log.info(
        f"Repeater command '{entry.id}' ({entry.label}) → `{ami_command}` "
        f"[target {rpt.id} · node {rpt.allstar_node} · AMI {rpt.ami.host}:{rpt.ami.port}]"
    )
    try:
        output = await client.run_command(ami_command)
    except AMICommandError as exc:
        log.error(
            f"Repeater command '{entry.id}' REJECTED by {rpt.id}'s Asterisk "
            f"(node {rpt.allstar_node} · AMI {rpt.ami.host}:{rpt.ami.port}): {exc}"
        )
        return await ctx.send(
            f"❌ **{entry.label}** was rejected by **{rpt.display_name}**'s Asterisk: `{exc}`"
        )
    except Exception as exc:
        log.error(
            f"Repeater command '{entry.id}' failed [target {rpt.id} · node {rpt.allstar_node} "
            f"· AMI {rpt.ami.host}:{rpt.ami.port}]: {exc}",
            exc_info=True,
        )
        return await ctx.send(f"❌ AMI error running **{entry.label}**: `{exc}`")

    result = output.strip()
    log.info(
        f"Repeater command '{entry.id}' completed on {rpt.id} (node {rpt.allstar_node}) — "
        + (f"output: {result[:200]!r}" if result else "no CLI output (normal for rpt fun / DTMF injection)")
    )
    msg = (f"📻 Ran **{entry.label}** on **{rpt.display_name}** "
           f"(Node `{rpt.allstar_node}`) {_target_note(rpt, how)}")
    if result:
        msg += f"\n```{result[:1500]}```"
    await ctx.send(msg)


async def _repeater_target_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=f"{r.display_name} ({r.id})", value=r.id)
        for r in cfg.repeaters if current.lower() in r.id.lower()
    ]

for _cmd in (link_cmd, unlink_cmd, unlink_all_cmd, monitor_node_cmd, repeater_cmd_cmd, tx_kill_cmd):
    _cmd.autocomplete("repeater")(_repeater_target_autocomplete)


@repeater_cmd_cmd.autocomplete("command")
async def _repeater_cmd_autocomplete(interaction: discord.Interaction, current: str):
    # Only offer commands valid for whichever repeater is currently active in
    # this guild, so operators don't even see options that would just bounce
    # with a "wrong repeater" error. The handler still enforces this for real
    # (autocomplete choices aren't a hard guarantee of what gets submitted).
    active_id = get_state(interaction.guild.id).preset if interaction.guild else None
    return [
        app_commands.Choice(name=f"{c.label} ({c.id})", value=c.id)
        for c in cfg.repeater_commands
        if (current.lower() in c.id.lower() or current.lower() in c.label.lower())
        and (active_id is None or c.valid_for(active_id))
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Ham Radio Utility Commands
# ─────────────────────────────────────────────────────────────────────────────

@bot.hybrid_command(name="qrz", description="Look up a callsign on QRZ.com.")
@app_commands.describe(callsign="The amateur radio callsign to look up")
async def qrz_cmd(ctx: commands.Context, callsign: str):
    if _qrz is None:
        return await ctx.send("❌ QRZ is not configured. Add `qrz:` to config.yaml.", ephemeral=True)
    await ctx.defer()
    try:
        data = await _qrz.lookup(callsign)
        e = _qrz_embed(data)
        await ctx.send(embed=e)
    except QRZError as exc:
        # Expected errors (not found, auth failure, etc.) — show cleanly to user
        await ctx.send(f"❌ {exc}")
    except Exception as exc:
        # Unexpected errors (network, parse, etc.) — log fully, show terse message
        log.error(f"QRZ lookup unexpected error for {callsign!r}: {exc}", exc_info=True)
        await ctx.send("❌ Unexpected error during QRZ lookup. Check the bot logs.")


def _qrz_embed(data: dict) -> discord.Embed:
    cs       = data["callsign"]
    names    = [p for p in [data.get("fname"), data.get("name")] if p]
    fullname = " ".join(names) or "N/A"
    loc_parts= [p for p in [data.get("addr2"), data.get("country")] if p]
    location = ", ".join(loc_parts) or "N/A"

    e = discord.Embed(
        title = f"📋 {cs}",
        url   = f"https://www.qrz.com/db/{cs}",
        color = discord.Color.from_rgb(30, 100, 220),
    )
    e.add_field(name="Name",     value=fullname,                           inline=True)
    e.add_field(name="Class",    value=data.get("license_class") or "N/A", inline=True)
    e.add_field(name="Grid",     value=data.get("grid")          or "N/A", inline=True)
    e.add_field(name="Location", value=location,                           inline=False)
    if data.get("expires"):
        e.add_field(name="License Expires", value=data["expires"], inline=True)
    if data.get("county") and data.get("state"):
        e.add_field(name="County", value=f"{data['county']}, {data['state']}", inline=True)
    if data.get("email"):
        e.add_field(name="Email",   value=data["email"],   inline=True)
    if data.get("trustee"):
        e.add_field(name="Trustee", value=data["trustee"], inline=True)
    if data.get("aliases"):
        e.add_field(name="Aliases", value=data["aliases"], inline=True)
    if data.get("image"):
        e.set_thumbnail(url=data["image"])
    e.set_footer(text="Data from QRZ.com")
    return e


@bot.hybrid_command(name="solar", description="Current solar conditions and HF propagation forecast.")
async def solar_cmd(ctx: commands.Context):
    await ctx.defer()
    try:
        data = await fetch_solar()
        await ctx.send(embed=_solar_embed(data))
    except Exception as exc:
        log.warning(f"Solar fetch failed: {exc}")
        await ctx.send(f"❌ Could not fetch solar data: `{exc}`")


@bot.hybrid_command(name="help", description="List all available bot commands.")
async def help_cmd(ctx: commands.Context):
    """Organised command reference for the K2BR Repeater Bot."""
    callsign = cfg.club.callsign
    prefix   = cfg.bot.prefix
    vhf_node = next((r.allstar_node for r in cfg.repeaters if r.id == "vhf"), "?")
    uhf_node = next((r.allstar_node for r in cfg.repeaters if r.id == "uhf"), "?")

    e = discord.Embed(
        title       = f"📖 {BOT_NAME} — Command Reference",
        description = (
            f"Use `/command` (slash) or `{prefix}command` (prefix).\n"
            f"Operator-role commands require the configured Discord role."
        ),
        color = discord.Color.blurple(),
    )

    # Each section is ONE field — well within Discord's 25-field embed limit
    # and leaves plenty of room for future commands.

    e.add_field(
        name="📻 Streaming",
        value=(
            "`/join` — Join your voice channel and start streaming\n"
            "`/leave` — Stop the stream and disconnect\n"
            "`/vhf` — Switch to (or start) VHF 146.745 MHz\n"
            "`/uhf` — Switch to (or start) UHF 448.775 MHz\n"
            "`/stream <name>` — Switch to any named preset (autocomplete)\n"
            "`/reconnect` — Force-restart the audio stream\n"
            "`/presets` — List all configured stream presets"
        ),
        inline=False,
    )

    e.add_field(
        name="🎛️ Control Panel",
        value=(
            "`/panel` — Post the button panel (▶ Start · ⏹ Stop · 🔄 · 📻 VHF · 📡 UHF)\n"
            "`/status` — Show stream status (ephemeral)"
        ),
        inline=False,
    )

    e.add_field(
        name="📡 Repeater Control  *(operator role)*",
        value=(
            f"`/link-repeaters` — Link VHF (Node {vhf_node}) ↔ UHF (Node {uhf_node})\n"
            "`/unlink-repeaters` — Unlink VHF and UHF\n"
            "`/link <node>` — Link active repeater to any AllStar node\n"
            "`/unlink <node>` — Unlink from a specific node\n"
            "`/unlink-all` — Disconnect all links on active repeater\n"
            "`/monitor-node <node>` — Listen-only connection to a node\n"
            "`/node-status` — Live AMI query of connected nodes\n"
            "`/repeater-cmd <name>` — Run a named HamVOIP action (autocomplete)"
        ),
        inline=False,
    )

    if cfg.tx.enabled:
        e.add_field(
            name="🎙️ Transmit (TX)",
            value=(
                "Authorized operators (config.yaml `tx_operators`) transmit by "
                "just talking normally in the voice channel — no command needed.\n"
                "`/tx-status` — Who (if anyone) currently holds TX on each repeater\n"
                "`/tx-kill` — *(operator role)* Immediately force-release TX on the active repeater"
            ),
            inline=False,
        )

    e.add_field(
        name="🔭 Ham Radio Utilities",
        value=(
            f"`/info` — {callsign} repeater info: frequencies, PL tones, location\n"
            "`/qrz <callsign>` — QRZ.com lookup: name, grid, license class\n"
            "`/solar` — Solar flux, K/A index, HF band conditions\n"
            "`/help` — Show this message"
        ),
        inline=False,
    )

    e.set_footer(text=f"{cfg.club.name} · {callsign} · {BOT_NAME}")
    await ctx.send(embed=e, ephemeral=True)


def _solar_embed(data: dict) -> discord.Embed:
    ICONS = {"Good": "🟢", "Fair": "🟡", "Poor": "🔴"}
    k_val  = data["k_index"]
    k_icon = "🟢" if k_val.isdigit() and int(k_val) <= 2 else (
             "🟡" if k_val.isdigit() and int(k_val) <= 4 else "🔴")

    e = discord.Embed(title="☀️ Solar Conditions & HF Propagation", color=discord.Color.gold())
    e.add_field(name="Solar Flux (SFI)", value=data["solar_flux"],                 inline=True)
    e.add_field(name="Sunspots",         value=data["sunspots"],                   inline=True)
    e.add_field(name="X-Ray",            value=data["x_ray"],                      inline=True)
    e.add_field(name=f"K-index {k_icon}", value=data["k_index"],                   inline=True)
    e.add_field(name="A-index",          value=data["a_index"],                    inline=True)
    e.add_field(name="Solar Wind",       value=f"{data['solar_wind']} km/s",       inline=True)

    if data["bands_day"]:
        day_str   = "\n".join(f"{ICONS.get(v,'⚪')} {b}: {v}" for b, v in data["bands_day"].items())
        night_str = "\n".join(f"{ICONS.get(v,'⚪')} {b}: {v}" for b, v in data["bands_night"].items())
        e.add_field(name="HF — Daytime",   value=day_str   or "N/A", inline=True)
        e.add_field(name="HF — Nighttime", value=night_str or "N/A", inline=True)

    if data["vhf"]:
        e.add_field(name="VHF Conditions", value="\n".join(data["vhf"][:4]), inline=False)

    e.set_footer(text=f"Source: hamqsl.com  ·  Updated: {data['updated']}")
    return e


# ─────────────────────────────────────────────────────────────────────────────
# Background Tasks
# ─────────────────────────────────────────────────────────────────────────────

@tasks.loop(seconds=30)
async def watchdog():
    """Reconnect any guild whose voice client is connected but not playing."""
    for guild in bot.guilds:
        vc = guild.voice_client
        if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused():
            gs = get_state(guild.id)
            log.warning(f"Watchdog: not playing in '{guild.name}' — reconnecting…")
            try:
                _play_paused(vc, _make_source(gs.preset, guild.id), after=lambda e, _vc=vc: _after_play(e, _vc))
                gs.reconnects += 1
                gs.streaming   = True
                if not gs.started_at:
                    gs.started_at = datetime.now(timezone.utc).timestamp()
                log.info(f"Watchdog reconnect OK [{guild.name}] (#{gs.reconnects})")
            except Exception as exc:
                log.error(f"Watchdog reconnect failed [{guild.name}]: {exc}")


@tasks.loop(seconds=3)
async def state_sync():
    """
    Keep gs.streaming in sync with the real voice client for every guild.

    "Streaming" means the repeater audio is genuinely flowing right now —
    Discord voice session active (playing OR correctly paused waiting for
    repeater activity, per _play_paused()) AND the underlying SIP/RTP
    connection to the repeater is actually CONNECTED. Both halves matter:
    without the SIP check, "Stream Up"/"Streaming Since" kept counting from
    the original /join time even while SIP was fully disconnected and
    retrying — RepeaterAudioSource.read() never returns empty bytes (always
    real audio or silence), so Discord's player stays "playing" regardless
    of whether the actual repeater connection behind it is healthy. The SIP
    state is the same signal already shown in the dashboard's "SIP" column
    and /status's "SIP Connection" field (see _audio_state_text()) — this
    just makes the streaming flag/uptime timers agree with it instead of
    only reflecting the Discord side.
    """
    for guild in bot.guilds:
        vc     = guild.voice_client
        gs     = get_state(guild.id)
        client = _monitor_clients.get(get_state(guild.id).preset)
        sip_connected = client is not None and client.state.name == "CONNECTED"
        gs.streaming = bool(vc and (vc.is_playing() or vc.is_paused())) and sip_connected


@tasks.loop(seconds=cfg.activity.poll_interval_seconds)
async def activity_feed():
    """
    Poll AllStar AMI for node connection changes and post to the activity channel.
    The NodeActivityMonitor initialises silently on first poll — no false
    "connected" events for nodes that were already linked at startup.
    """
    # Disabled repeaters are off entirely — no AMI polling either, so a
    # node that's down for maintenance doesn't generate link/unlink noise.
    await node_monitor.poll(cfg.enabled_repeaters(), bot, cfg.activity_channel_id_for)


@tasks.loop(seconds=20)
async def sip_health_watch():
    """
    Watch every repeater's always-on SIP monitor and post a warning to
    that repeater's activity channel if it's been stuck RECONNECTING for
    longer than cfg.sip_health.alert_after_seconds — so a dropped
    Asterisk-side connection doesn't go unnoticed until someone checks
    /status. Posts a recovery message once it reconnects, and only posts
    once per episode (tracked via _sip_alerted) rather than every tick.
    Monitors run whether or not anything is being played, so every defined
    repeater is covered full time.
    """
    if not cfg.has_activity_channels():
        return

    from repeater_audio import ConnectionState

    watch_list = []
    for rpt_id, client in list(_monitor_clients.items()):
        rpt      = cfg.repeater_by_id(rpt_id)
        rpt_name = rpt.display_name if rpt else rpt_id
        watch_list.append((rpt_id, rpt, rpt_name, "monitor", client))

    for alert_key, rpt, rpt_name, gname, client in watch_list:
        channel = _activity_channel(rpt)
        alerted = _sip_alerted.get(alert_key, False)

        stuck = (
            client.state == ConnectionState.RECONNECTING
            and client.state_duration >= cfg.sip_health.alert_after_seconds
        )

        if stuck and not alerted:
            _sip_alerted[alert_key] = True
            log.warning(f"SIP health alert: {rpt_name} stuck RECONNECTING [{gname}]")
            if channel is None:
                continue
            try:
                await channel.send(
                    f"⚠️ **SIP audio for {rpt_name}** has been reconnecting for over "
                    f"{cfg.sip_health.alert_after_seconds}s in **{gname}** — "
                    f"check the repeater's Asterisk/SIP side."
                )
            except Exception as exc:
                log.warning(f"Activity channel post failed (SIP health alert): {exc}")

        elif client.state == ConnectionState.CONNECTED and alerted:
            _sip_alerted[alert_key] = False
            log.info(f"SIP health recovered: {rpt_name} [{gname}]")
            if channel is None:
                continue
            try:
                await channel.send(f"✅ **SIP audio for {rpt_name}** recovered in **{gname}**.")
            except Exception as exc:
                log.warning(f"Activity channel post failed (SIP health recovery): {exc}")


@tasks.loop(seconds=1)
async def tx_lock_watch():
    """
    Enforce TX safety limits every second (tight interval — these are safety
    cutoffs, not informational polling):
      - release a lock after cfg.tx.release_hangover_seconds of no new
        Discord packets from its holder (they stopped talking)
      - force-release after cfg.tx.max_transmission_seconds regardless of
        continued activity — stuck-key protection, defense in depth
        alongside whatever timeout timer the node's own rpt.conf enforces
    """
    if not cfg.tx.enabled or not _tx_locks:
        return
    now = time.time()
    for rpt_id, lock in list(_tx_locks.items()):
        if now - lock.started_at > cfg.tx.max_transmission_seconds:
            _tx_locks.pop(rpt_id, None)
            _tx_release_ptt(rpt_id)
            log.warning(
                f"TX timeout: {lock.callsign} force-released from {rpt_id} "
                f"after {cfg.tx.max_transmission_seconds}s"
            )
            await _post_tx_event(rpt_id, lock.callsign, "timeout")
        elif now - lock.last_packet_at > cfg.tx.release_hangover_seconds:
            _tx_locks.pop(rpt_id, None)
            _tx_release_ptt(rpt_id)
            log.info(f"TX: released {lock.callsign} on {rpt_id}")
            await _post_tx_event(rpt_id, lock.callsign, "released")


@watchdog.before_loop
@state_sync.before_loop
@activity_feed.before_loop
@sip_health_watch.before_loop
@tx_lock_watch.before_loop
async def _wait_ready():
    await bot.wait_until_ready()


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def _dashboard_worker(live: Live, stop: threading.Event) -> None:
    while not stop.is_set():
        live.update(build_dashboard())
        time.sleep(1)


def main() -> None:
    if not cfg.bot.token or cfg.bot.token == "YOUR_DISCORD_BOT_TOKEN":
        console.print(
            "[bold red]✗ Bot token not set.[/bold red]  "
            "Edit [bold]config.yaml[/bold] → bot.token and try again."
        )
        sys.exit(1)

    repeater_summary = "  ".join(
        f"[yellow]{r.id}[/yellow] [dim]"
        f"{r.sip_audio.host + ':' + str(r.sip_audio.port) if r.sip_audio else 'no audio configured'}"
        f"[/dim]"
        for r in cfg.repeaters
    )
    console.print(Panel.fit(
        f"[bold magenta]{BOT_NAME}[/bold magenta]  v{BOT_VERSION}\n"
        f"Club      : {cfg.club.name} ({cfg.club.callsign})\n"
        f"Repeaters : {repeater_summary}\n"
        f"Log file  : {cfg.bot.log_file}\n"
        f"Role lock : {cfg.bot.controller_role or '[dim]none — everyone can control[/dim]'}\n"
        f"Auto-join : {cfg.bot.auto_join_channel_id or '[dim]disabled[/dim]'}",
        title="[bold]Starting up[/bold]",
        border_style="magenta",
    ))

    # Write version banner to the log file so each run is clearly identified
    log.info(f"{'─' * 60}")
    log.info(f"{BOT_NAME}  v{BOT_VERSION}  —  starting up")
    log.info(f"Club: {cfg.club.name} ({cfg.club.callsign})  ·  Repeaters: {[r.id for r in cfg.repeaters]}")
    log.info(f"{'─' * 60}")

    stop = threading.Event()
    with Live(build_dashboard(), console=console, refresh_per_second=0.5, screen=False) as live:
        worker = threading.Thread(target=_dashboard_worker, args=(live, stop), daemon=True)
        worker.start()
        try:
            sats = cfg.satellite_repeaters()
            if not sats:
                # Single-app path — identical to previous releases.
                bot.run(cfg.bot.token, log_handler=None)
            else:
                # Multi-app path: primary + one headless SatelliteBot per
                # dedicated-token repeater, all on one asyncio loop in this
                # process so they share state (TX locks, registries, config).
                # (bot.run()'s logging setup is skipped on the .start() path,
                # which matches the log_handler=None we pass above.)
                try:
                    asyncio.run(_run_all_bots(sats))
                except KeyboardInterrupt:
                    pass
        finally:
            stop.set()
            _stop_monitors()


async def _run_all_bots(sats) -> None:
    """
    Run the primary bot and all satellites concurrently. If any bot exits
    (bad token, fatal gateway error), cancel the rest and shut down — a
    partially-running fleet is harder to notice and debug than a clean exit.
    """
    # Set the shared loop reference immediately, not in the primary's
    # on_ready — satellites can be connected and receiving SIP audio before
    # the primary finishes its handshake, and thread-side callbacks
    # (on_transmission, TX events) drop events while _loop is None.
    global _loop
    _loop = asyncio.get_running_loop()
    _ensure_monitors()

    for rpt in sats:
        _satellites[rpt.id] = SatelliteBot(rpt)
        log.info(f"Satellite configured: '{rpt.id}' → channel {rpt.discord.channel_id}")
        # Legal but almost certainly unintended: a satellite pointed at the
        # same channel as a primary repeater means two bots audible at once.
        for pr in cfg.primary_repeaters():
            if pr.discord.channel_id and pr.discord.channel_id == rpt.discord.channel_id:
                log.warning(
                    f"Satellite '{rpt.id}' shares channel {rpt.discord.channel_id} "
                    f"with primary repeater '{pr.id}' — both will be audible "
                    f"in that channel simultaneously."
                )

    async def _start(client: discord.Client, token: str, label: str):
        try:
            await client.start(token)
        finally:
            log.warning(f"Bot task exited: {label}")

    tasks_ = [asyncio.create_task(_start(bot, cfg.bot.token, "primary"))]
    tasks_ += [
        asyncio.create_task(_start(s, s.rpt.discord.token, f"satellite:{rid}"))
        for rid, s in _satellites.items()
    ]
    try:
        done, pending = await asyncio.wait(tasks_, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc:
                raise exc
    finally:
        closers = [c.close() for c in [bot, *_satellites.values()] if not c.is_closed()]
        if closers:
            await asyncio.gather(*closers, return_exceptions=True)


if __name__ == "__main__":
    main()
