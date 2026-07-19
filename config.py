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

config.py — Loads config.yaml into typed dataclasses.

Usage:
    from config import cfg
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# Single source of truth for the bot version — used by the startup banner,
# log lines, and the QRZ API user-agent, so a release bump is one edit.
BOT_VERSION = "1.0.0"

CONFIG_PATH = Path("config.yaml")


# ─── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class BotConfig:
    token:                str
    prefix:               str
    log_file:             str
    log_file_level:       str       # file-log verbosity: DEBUG (everything)
                                      # or INFO for normal operation. Console
                                      # is always
                                      # INFO+. Flip to DEBUG when chasing an
                                      # issue; the SIP heartbeat aggregator
                                      # keeps even DEBUG mode readable.
    controller_role:      str
    auto_join_channel_id: int
    guild_id:             int       # 0 = no primary guild
    sip_debug_verbose:    bool      # False (default) = filtered rfcvoip
                                      # logging: short summaries only, no raw
                                      # protocol dumps. True = full detail —
                                      # useful when actively chasing a SIP
                                      # issue, but produces enormous log
                                      # volume over a long-running session
                                      # (a full Status/Headers/Body/Raw dump
                                      # fires on every REGISTER refresh,
                                      # ~every 120s for as long as the call
                                      # is up). See repeater_audio.py's
                                      # _bridge_rfcvoip_debug().


@dataclass
class Location:
    name:     str
    address:  str
    maps_url: str


@dataclass
class ClubConfig:
    name:     str
    callsign: str
    website:  str
    trustee:  str
    location: Location


@dataclass
class RepeaterAMIConfig:
    host:     str
    port:     int
    username: str
    password: str


@dataclass
class SIPAudioConfig:
    host:      str
    port:      int
    username:  str
    password:  str
    extension: str    # extension / node number to call in Asterisk dialplan
    local_ip:  str    # bot's LAN IP for RTP; auto-detected if ""
    local_sip_port: int = 0   # LOCAL UDP port this client binds for SIP.
                              # 0 = auto-assigned uniquely per repeater at
                              # startup (5060, 5062, ...). Every always-on
                              # monitor needs its OWN local port — two
                              # clients binding 5060 on the same machine is
                              # exactly the WinError 10048 startup loop.


@dataclass
class RepeaterDiscordConfig:
    """
    Per-repeater Discord binding. Fields inherit from the top-level
    config when omitted (token ← bot.token, channel_id ←
    bot.auto_join_channel_id, activity_channel_id ← activity.channel_id),
    so a config with no `discord:` blocks behaves exactly as before:
    every repeater on the one primary bot, sharing one voice channel and
    one activity channel, preset-switchable.

    activity_channel_id gives this repeater its own text channel for
    activity posts (transmissions, node link/unlink, SIP health, TX
    events) — resolved through Config.activity_channel_id_for(), which
    falls back to the global activity.channel_id when unset.

    A repeater whose token differs from bot.token runs as a *satellite*:
    a second headless Discord application in the same process that joins
    its own channel and streams just that repeater — the only way to get
    two simultaneous voice streams in one guild, since Discord allows one
    voice connection per guild per bot application.
    """
    token:               str
    channel_id:          int
    activity_channel_id: int = 0   # 0 = fall back to activity.channel_id

    def is_dedicated(self, primary_token: str) -> bool:
        return bool(self.token) and self.token != primary_token


@dataclass
class RepeaterConfig:
    id:            str    # also doubles as the stream/preset selector (e.g. "vhf")
    display_name:  str
    frequency_mhz: float
    offset_mhz:    float
    pl_hz:         float
    allstar_node:  str
    enabled:       bool   # False = don't start this repeater's always-on SIP
                            # monitor at boot: no connection, no recording, no
                            # activity posts, no health alerts, and it can't be
                            # played to a voice channel. For a repeater that's
                            # down for maintenance, or one staged in config
                            # before its Asterisk side is ready. Defaults True,
                            # so omitting it keeps normal behavior.
    ami:           Optional[RepeaterAMIConfig]
    sip_audio:     Optional[SIPAudioConfig]    # None = repeater has no audio path configured
    discord:       "RepeaterDiscordConfig" = None  # always filled in by load()


@dataclass
class ActivityConfig:
    channel_id:            int
    poll_interval_seconds: int
    vad_rms_threshold:     int    # audioop.rms() energy threshold for local-PTT
                                   # detection on the activity feed. Site-dependent —
                                   # see repeater_audio.py's VAD docs and tune by
                                   # watching k2br_bot.log while keying up.
    vad_hangover_seconds:  float  # how long a pause between words/sentences is
                                   # tolerated before a transmission is considered
                                   # over. Also site/operator-dependent — too short
                                   # and a single continuous transmission gets
                                   # split into several logged fragments every
                                   # time someone pauses to breathe.
    record_transmissions: bool    # attach a WAV recording of each completed
                                    # transmission to its activity-channel
                                    # message. Off by default — storage and
                                    # privacy are a real consideration before
                                    # turning this on, not just a technical one.
    max_recording_seconds: float  # hard cap on recording length regardless
                                    # of how long the actual transmission ran
                                    # — protects against a stuck-open carrier
                                    # producing an unbounded WAV file. The
                                    # reported duration in the activity
                                    # message is never affected by this cap,
                                    # only the attached recording is.


@dataclass
class SIPHealthConfig:
    alert_after_seconds: int    # how long a guild's SIP audio can sit RECONNECTING
                                  # before we post a warning to the activity channel


# Placeholder sentinel for repeater_commands entries the club hasn't filled in
# yet — see the "Repeater Commands" section of config.yaml for why this isn't
# pre-populated with a guessed DTMF/function code.
REPEATER_COMMAND_PLACEHOLDER = "CHANGE_ME"


@dataclass
class RepeaterCommand:
    id:          str    # slash-command autocomplete value, e.g. "time"
    label:       str    # human-readable name, e.g. "Announce Time"
    description: str
    command:     str    # AMI "Command" string; {node} is substituted with the
                          # active repeater's allstar_node at run time
    repeaters:   list[str]    # which repeater id(s) this is valid for (by their
                                # DTMF/function-table config); empty = all repeaters.
                                # Function tables (rpt.conf [functionsNNNNN]) are
                                # per-node, so a code valid on one repeater isn't
                                # guaranteed to mean the same thing — or exist at
                                # all — on another. Scope accordingly.

    def valid_for(self, rpt_id: str) -> bool:
        return not self.repeaters or rpt_id in self.repeaters


@dataclass
class QRZConfig:
    username: str
    api_key:  str


@dataclass
class AsteriskConfig:
    enabled:       bool
    operator_role: str


# Deliberately permissive amateur-radio callsign format check. This is a
# sanity check to catch typos in config.yaml, NOT an authorization mechanism —
# callsigns typed anywhere are inherently spoofable, so the real gate is
# "is this Discord user ID in tx_operators", not "does a string look right".
# Covers common ITU-style formats (K2BR, W1AW, VE3ABC, G0ABC, 9A1ABC, ...);
# not exhaustive of every national format, and doesn't need to be.
CALLSIGN_FORMAT_RE = re.compile(r"^[A-Z0-9]{1,2}[0-9][A-Z]{1,4}$")


@dataclass
class TxOperator:
    discord_user_id: int
    callsign:        str


@dataclass
class TxConfig:
    enabled:                   bool
    max_transmission_seconds:  int    # hard bot-side cutoff — defense in depth
                                        # alongside whatever timeout your node's
                                        # own rpt.conf `totime` already enforces
    release_hangover_seconds:  float   # how long to keep the carrier up after
                                        # Discord packets stop, to bridge a
                                        # natural mid-sentence pause rather than
                                        # chopping the transmission
    verify_with_qrz_on_startup: bool   # sanity-check each configured callsign
                                        # against QRZ at startup (warns only,
                                        # never blocks — see on_ready in
                                        # allstar_discord_bot.py)
    ptt_key_dtmf:   str = "*99"        # app_rpt phone-mode PTT assert. Sent as
                                        # RFC2833 telephone-events when a TX
                                        # lock is acquired; without it app_rpt
                                        # silently discards caller audio.
                                        # Requires the dialplan to hand the
                                        # call to Rpt(node|P) (or |D).
    ptt_unkey_dtmf: str = "#"          # PTT release, sent on every TX release
                                        # path (hangover, timeout, /tx-kill,
                                        # disconnects). Empty string disables
                                        # both if your dialplan keys some
                                        # other way.


@dataclass
class Config:
    bot:               BotConfig
    club:              ClubConfig
    repeaters:         list[RepeaterConfig]
    default_preset:    str    # id of the first configured repeater
    activity:          ActivityConfig
    sip_health:        SIPHealthConfig
    repeater_commands: list[RepeaterCommand]
    tx:                TxConfig
    tx_operators:      list[TxOperator]
    qrz:               Optional[QRZConfig]
    asterisk:          AsteriskConfig

    # ── Helpers ───────────────────────────────────────────────────────────────

    def repeater_by_id(self, rpt_id: str) -> Optional[RepeaterConfig]:
        return next((r for r in self.repeaters if r.id == rpt_id), None)

    def enabled_repeaters(self) -> list[RepeaterConfig]:
        """Repeaters whose monitors should run (see RepeaterConfig.enabled)."""
        return [r for r in self.repeaters if r.enabled]

    def primary_repeaters(self) -> list[RepeaterConfig]:
        """Repeaters served by the primary bot application (bot.token)."""
        return [r for r in self.repeaters
                if not r.discord.is_dedicated(self.bot.token)]

    def satellite_repeaters(self) -> list[RepeaterConfig]:
        """Repeaters with their own dedicated Discord application token."""
        return [r for r in self.repeaters
                if r.discord.is_dedicated(self.bot.token)]

    def activity_channel_id_for(self, rpt: Optional[RepeaterConfig]) -> int:
        """
        Activity channel for a repeater: its own discord.activity_channel_id
        if set, else the global activity.channel_id. None (repeater unknown)
        resolves to the global channel. 0 = no channel configured.
        """
        if rpt is not None and rpt.discord and rpt.discord.activity_channel_id:
            return rpt.discord.activity_channel_id
        return self.activity.channel_id

    def has_activity_channels(self) -> bool:
        """True if any activity channel (global or per-repeater) is set."""
        return bool(self.activity.channel_id) or any(
            r.discord and r.discord.activity_channel_id for r in self.repeaters
        )

    def repeater_command_by_id(self, cmd_id: str) -> Optional[RepeaterCommand]:
        return next((c for c in self.repeater_commands if c.id == cmd_id), None)

    def tx_operator_by_discord_id(self, user_id: int) -> Optional[TxOperator]:
        return next((o for o in self.tx_operators if o.discord_user_id == user_id), None)


# ─── Loader ───────────────────────────────────────────────────────────────────

def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy config.example.yaml to {path.name} and fill in "
            f"your own values (bot token, AMI passwords, etc.) before running the bot."
        )
    raw = yaml.safe_load(path.read_text())

    b = raw["bot"]
    bot_cfg = BotConfig(
        token                = b["token"],
        prefix               = b.get("prefix", "!"),
        log_file             = b.get("log_file", "k2br_bot.log"),
        log_file_level       = str(b.get("log_file_level", "DEBUG")).upper(),
        controller_role      = b.get("controller_role", ""),
        auto_join_channel_id = int(b.get("auto_join_channel_id", 0) or 0),
        guild_id             = int(b.get("guild_id", 0) or 0),
        sip_debug_verbose    = bool(b.get("sip_debug_verbose", False)),
    )

    c   = raw["club"]
    loc = c["location"]
    club_cfg = ClubConfig(
        name     = c["name"],
        callsign = c["callsign"],
        website  = c.get("website", ""),
        trustee  = c.get("trustee", ""),
        location = Location(
            name     = loc["name"],
            address  = loc.get("address", ""),
            maps_url = loc.get("maps_url", ""),
        ),
    )

    repeaters: list[RepeaterConfig] = []
    for r in raw.get("repeaters", []):
        ami_raw = r.get("ami")
        ami_cfg = (
            RepeaterAMIConfig(
                host     = ami_raw["host"],
                port     = int(ami_raw.get("port", 5038)),
                username = ami_raw["username"],
                password = ami_raw["password"],
            )
            if ami_raw else None
        )
        sip_raw = r.get("sip_audio")
        sip_cfg = (
            SIPAudioConfig(
                host      = sip_raw["host"],
                port      = int(sip_raw.get("port", 5060)),
                username  = sip_raw["username"],
                password  = sip_raw["password"],
                extension = str(sip_raw["extension"]),
                local_ip  = sip_raw.get("local_ip", ""),
                local_sip_port = int(sip_raw.get("local_sip_port", 0) or 0),
            )
            if sip_raw and sip_raw.get("host") else None
        )
        # Per-repeater Discord binding, inheriting from the top-level bot
        # config — see RepeaterDiscordConfig. Absent block = today's exact
        # behavior (primary token, shared auto-join channel).
        d_raw = r.get("discord") or {}
        discord_cfg = RepeaterDiscordConfig(
            token               = str(d_raw.get("token", "") or "") or bot_cfg.token,
            channel_id          = int(d_raw.get("channel_id", 0) or 0) or bot_cfg.auto_join_channel_id,
            activity_channel_id = int(d_raw.get("activity_channel_id", 0) or 0),
        )
        repeaters.append(RepeaterConfig(
            id            = r["id"],
            display_name  = r["display_name"],
            frequency_mhz = float(r["frequency_mhz"]),
            offset_mhz    = float(r["offset_mhz"]),
            pl_hz         = float(r["pl_hz"]),
            allstar_node  = str(r.get("allstar_node", "") or ""),
            enabled       = bool(r.get("enabled", True)),
            ami           = ami_cfg,
            sip_audio     = sip_cfg,
            discord       = discord_cfg,
        ))

    default_preset = repeaters[0].id if repeaters else ""

    act = raw.get("activity", {})
    activity_cfg = ActivityConfig(
        channel_id            = int(act.get("channel_id", 0) or 0),
        poll_interval_seconds = int(act.get("poll_interval_seconds", 15)),
        vad_rms_threshold     = int(act.get("vad_rms_threshold", 400)),
        vad_hangover_seconds  = float(act.get("vad_hangover_seconds", 1.5)),
        record_transmissions  = bool(act.get("record_transmissions", False)),
        max_recording_seconds = float(act.get("max_recording_seconds", 300.0)),
    )

    sh = raw.get("sip_health", {})
    sip_health_cfg = SIPHealthConfig(
        alert_after_seconds = int(sh.get("alert_after_seconds", 60)),
    )

    repeater_commands: list[RepeaterCommand] = []
    for rc in raw.get("repeater_commands", []):
        repeater_commands.append(RepeaterCommand(
            id          = rc["id"],
            label       = rc.get("label", rc["id"]),
            description = rc.get("description", ""),
            command     = str(rc.get("command", REPEATER_COMMAND_PLACEHOLDER)),
            repeaters   = list(rc.get("repeaters", []) or []),
        ))

    qrz_raw = raw.get("qrz")
    qrz_cfg = (
        QRZConfig(username=qrz_raw["username"], api_key=qrz_raw["api_key"])
        if qrz_raw and qrz_raw.get("api_key") else None
    )

    ast = raw.get("asterisk", {})
    asterisk_cfg = AsteriskConfig(
        enabled       = bool(ast.get("enabled", False)),
        operator_role = ast.get("operator_role", ""),
    )

    tx_raw = raw.get("tx", {})
    tx_cfg = TxConfig(
        enabled                    = bool(tx_raw.get("enabled", False)),
        max_transmission_seconds   = int(tx_raw.get("max_transmission_seconds", 180)),
        release_hangover_seconds   = float(tx_raw.get("release_hangover_seconds", 1.0)),
        verify_with_qrz_on_startup = bool(tx_raw.get("verify_with_qrz_on_startup", False)),
        ptt_key_dtmf               = str(tx_raw.get("ptt_key_dtmf", "*99")),
        ptt_unkey_dtmf             = str(tx_raw.get("ptt_unkey_dtmf", "#")),
    )

    tx_operators: list[TxOperator] = []
    for op in raw.get("tx_operators", []):
        callsign = str(op["callsign"]).strip().upper()
        if not CALLSIGN_FORMAT_RE.match(callsign):
            print(
                f"WARNING: tx_operators entry {op.get('discord_user_id')!r} has a "
                f"callsign {callsign!r} that doesn't match the expected format — "
                f"double check config.yaml for a typo. (This is a sanity check "
                f"only, not enforced.)"
            )
        tx_operators.append(TxOperator(
            discord_user_id = int(op["discord_user_id"]),
            callsign        = callsign,
        ))

    return Config(
        bot               = bot_cfg,
        club              = club_cfg,
        repeaters         = repeaters,
        default_preset    = default_preset,
        activity          = activity_cfg,
        sip_health        = sip_health_cfg,
        repeater_commands = repeater_commands,
        tx                = tx_cfg,
        tx_operators      = tx_operators,
        qrz               = qrz_cfg,
        asterisk          = asterisk_cfg,
    )


# Module-level singleton
cfg: Config = load()
