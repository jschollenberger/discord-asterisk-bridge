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

repeater_audio.py — SIP/RTP audio client for AllStar/HamVOIP repeaters.

Provides the bot's sole repeater-audio path: a direct SIP/RTP connection
(~80ms latency) that also lays the foundation for future two-way audio
(Discord users transmitting on the repeater).

Uses rfcvoip (https://github.com/TAbdiukov/rfcvoip), a maintained continuation
of PyVoIP — the original PyVoIP project has been frozen for a long time.
rfcvoip is GPLv3-licensed (PyVoIP was MIT); this only matters if this bot is
ever redistributed outside the club, not for self-hosting.

We pin the phone's *public* audio format to 16-bit signed PCM @ 8000 Hz mono
(see VoIPPhone(audio_bit_depth=..., audio_sample_rate=..., audio_channels=...)
below). rfcvoip decodes whatever codec was negotiated (PCMU here) into that
format for us, so call.read_audio() already hands back linear PCM — no manual
G.711 μ-law decode step is needed on our end.

Beyond audio, this module also exposes:
  - Connection state + how long the client has been in that state
    (state / state_duration), so callers can alert on stuck reconnects.
  - Lightweight energy-threshold voice-activity detection (VAD) on the RX
    stream, reported via an on_transmission(duration_seconds) callback fired
    once per completed transmission — useful for activity logging without
    depending on AMI's node-link polling (which has no idea when someone is
    actually talking).

Audio paths
-----------
RX (this file):   Asterisk RTP (PCMU) → rfcvoip decode → PCM 8kHz mono
                  → PCM 48kHz stereo → discord.py AudioSource

TX (future stub): discord.py AudioSink → PCM 48kHz stereo → PCM 8kHz mono
                  → rfcvoip encode (PCMU) → Asterisk RTP
                  (send_frame is a documented no-op until TX is implemented)

HamVOIP setup required (per repeater machine)
----------------------------------------------
/etc/asterisk/sip_custom.conf:

    [discord-vhf]            ; or discord-uhf
    type=friend
    secret=CHOOSE_A_PASSWORD
    host=dynamic
    context=discord-audio
    disallow=all
    allow=ulaw
    dtmfmode=rfc2833
    qualify=yes
    callerid="DISCORD" <50420>   ; shows as caller ID in Asterisk (core show
                                   ; channels, CDR, console) — this is set
                                   ; here, not in our Python code, because
                                   ; rfcvoip's own INVITE doesn't currently
                                   ; send a display name at all, and Asterisk
                                   ; substitutes this peer-level value by
                                   ; default (trustrpid=no is chan_sip's
                                   ; default, so it doesn't trust — or need —
                                   ; anything the client itself claims).
                                   ; Use the matching node number per peer
                                   ; (53209 for discord-uhf).

/etc/asterisk/extensions_custom.conf:

    [discord-audio]
    exten => 50420,1,Answer()
    exten => 50420,n,Wait(1)
    exten => 50420,n,rpt(50420,P)
    exten => 50420,n,Hangup()

    ; Repeat for UHF node on its own machine:
    exten => 53209,1,Answer()
    exten => 53209,n,Wait(1)
    exten => 53209,n,rpt(53209,P)
    exten => 53209,n,Hangup()

    ; The ",P" matters: bare rpt(NODE) runs in "normal endpoint mode", which
    ; verifies the connecting client's source IP against AllStar's node/IP
    ; database — a check this bot's SIP client will always fail, since it's
    ; a plain SIP UA (authenticated via sip_custom.conf's secret=, not a
    ; registered AllStar peer node). Without an option, Asterisk answers the
    ; call and then immediately sends BYE (visible in k2br_bot.log as a call
    ; that connects, receives ~1 audio frame, and ends within ~1 second).
    ;
    ; P = "Phone Control mode" — full audio access for a non-node client,
    ; the same category AllStarLink's own Telephone Portal uses for regular
    ; phone/SIP callers. Confirmed working against this club's actual UHF
    ; node (53209) — this is the verified value, not a guess.
    ;
    ; (X — "normal endpoint but bypassing further authentication checks" —
    ; was tried first based on AllStarLink's own docs and did NOT work here;
    ; P is what's actually confirmed against real hardware. Worth knowing if
    ; you're cross-referencing AllStarLink's docs yourself and see X
    ; recommended elsewhere — P is what worked for this specific setup.)
    ;
    ; Sources: https://allstarlink.github.io/config/extensions_conf/
    ;          https://www.voip-info.org/asterisk-cmd-rpt/

After editing: asterisk -rx "sip reload" && asterisk -rx "dialplan reload"
"""
from __future__ import annotations

import audioop
import io
import logging
import platform
import random
import socket
import threading
import time
import wave
from collections import deque
from enum import Enum, auto
from typing import Callable, Optional

import discord

log = logging.getLogger("k2br.sip")

# ── Audio constants ───────────────────────────────────────────────────────────

# discord.py expects: 20ms frames · 48kHz · stereo · s16le
DISCORD_FRAME_BYTES = 3840        # 48000 Hz × 2 ch × 2 bytes × 0.020 s
DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS    = 2

# AllStar/HamVOIP sends G.711 μ-law on the wire (8kHz mono), but we configure
# rfcvoip's *public* audio format to 16-bit signed linear PCM @ 8kHz mono, so
# call.read_audio() already returns decoded linear PCM at this frame size —
# not raw μ-law bytes. See RepeaterAudioClient._connect_and_stream().
RTP_SAMPLE_RATE     = 8_000
RTP_FRAME_BYTES     = 320         # 20ms @ 8kHz mono, 16-bit signed PCM (2 bytes/sample)
REGISTRATION_TIMEOUT_SECONDS = 10.0   # backstop for a peer that never answers REGISTER
REGISTRATION_POLL_SECONDS    = 0.05  # 20 Hz — confirms as soon as the 200 OK lands
TEARDOWN_SETTLE_SECONDS = 3.0   # post-phone.stop() grace before rebuild — see _connect_and_stream cleanup
FRAME_MS            = 20

SILENCE = bytes(DISCORD_FRAME_BYTES)

# Audio buffer: up to ~2 s of frames.  deque.append/popleft are GIL-protected
# so they are thread-safe without an explicit lock.
BUFFER_MAXFRAMES = 100

# ── Voice-activity detection (VAD) ────────────────────────────────────────────
# Simple energy-threshold VAD on the 8kHz mono RX stream, used to report
# completed transmissions (on_transmission) and drive Discord's speaking
# indicator (on_speaking_change). The RMS threshold AND the hangover window
# are both site-dependent — threshold on noise floor/receiver level, hangover
# on how people actually talk (pause length between words/sentences varies a
# lot by operator). Both are passed in per-client from config.yaml rather
# than fixed constants — an earlier version hardcoded hangover at 300ms on
# the assumption it wouldn't need tuning; in practice that was too short and
# split single continuous transmissions into several multi-second fragments
# whenever someone paused mid-sentence to breathe. VAD_MIN_TX_SECONDS is the
# one constant left fixed — it just filters obvious noise blips, not a
# behavior that varies by operator the way hangover does.
VAD_MIN_TX_SECONDS  = 0.5     # ignore shorter blips as VAD noise, not real transmissions


_rfcvoip_debug_bridged = False
_sip_debug_verbose     = False   # checked live by _bridged_debug on every call,
                                   # not baked in at patch time — see _bridge_rfcvoip_debug()


def _bridge_rfcvoip_debug(verbose: bool = False) -> None:
    """
    Redirect rfcvoip's internal debug() calls into our own k2br.sip logger,
    instead of the raw print() they use by default.

    Why this matters: rfcvoip.DEBUG (a module flag) gates its internal
    debug() calls, which are otherwise completely silent — meaning things
    like "REGISTRATION FAILED", auth-challenge details, and the actual SIP
    response codes/reasons from Asterisk are normally invisible to us. That's
    a real gap: our own logging (added around SIP connect/register/call)
    only sees the *outcome* of rfcvoip's internals, not *why* something
    failed inside them.

    We don't just set rfcvoip.DEBUG = True, because its debug() does a raw
    print() — that would fight with the Rich Live dashboard's terminal
    control codes in main() and likely corrupt the console output. Instead
    we monkeypatch debug() itself to route through our logger, which
    respects the existing console(INFO)/file(DEBUG) split.

    Must patch each submodule's own bound `debug` name individually
    (SIP.py, RTP.py, VoIP/VoIP.py) — they each did `from rfcvoip import
    debug`, so reassigning rfcvoip.debug alone does not affect their
    already-bound references. Verified this directly rather than assuming.

    By default (verbose=False) this filters out the two things that make
    rfcvoip's internal logging genuinely unusable for normal, long-running
    operation: full multi-line Status/Headers/Body/Raw protocol dumps (one
    fires on every REGISTER refresh — every ~120s for as long as the call
    is up) and raw SIP message byte dumps. Both get replaced with nothing
    or a one-line summary; short, already-compact messages ("New register
    thread", "SIP response ... status=200 OK", "REGISTRATION FAILED") pass
    through unchanged either way. Pass verbose=True (config.yaml's
    bot.sip_debug_verbose) to get the full, unfiltered firehose back for
    actively debugging a SIP issue — same tradeoff this bridge itself was
    originally built to make visible in the first place.

    The verbosity flag is checked live on every call (not captured at patch
    time), and this function updates it on every call even if already
    patched — so flipping bot.sip_debug_verbose and restarting the bot is
    enough to change behavior without needing new code.
    """
    global _rfcvoip_debug_bridged, _sip_debug_verbose
    _sip_debug_verbose = verbose
    if _rfcvoip_debug_bridged:
        return
    try:
        import rfcvoip
        import rfcvoip.SIP as _rfcvoip_sip
        import rfcvoip.RTP as _rfcvoip_rtp
        import rfcvoip.VoIP.VoIP as _rfcvoip_voip

        def _bridged_debug(s, e=None):
            if _sip_debug_verbose:
                log.debug(f"[rfcvoip] {s}")
                if e is not None:
                    log.debug(f"[rfcvoip] {e}")
                return

            # Quiet mode (default). rfcvoip calls debug() with `s` as either
            # a str OR a raw bytes object depending on call site — never
            # assume which. An earlier version of this filter called
            # s.startswith("b'") unconditionally, which raised a real
            # TypeError whenever s was actually bytes (bytes.startswith()
            # requires a bytes argument, not str). That exception then
            # propagated into rfcvoip's own calling code, which reported it
            # as a fake "REGISTRATION ERROR" — including on registrations
            # Asterisk had genuinely just answered with 200 OK. Checking
            # isinstance() first avoids ever calling a str-only method on a
            # bytes object (or vice versa).
            if isinstance(s, (bytes, bytearray)):
                return   # raw byte dump — carries no info beyond the
                          # structured "Status: ..." dump already logged a
                          # moment earlier for the same event
            # The structured Status/Headers/Body/Raw dump — keep just its
            # first line ("Status: 200 OK", "Status: 407 Proxy
            # Authentication Required", etc.), which is the actually
            # useful part for normal operation.
            if isinstance(s, str) and "\n\nHeaders:\n" in s:
                log.debug(f"[rfcvoip] {s.splitlines()[0]}")
                return
            # Everything else is already a short, single-line message —
            # pass through unchanged.
            log.debug(f"[rfcvoip] {s}")
            if e is not None:
                log.debug(f"[rfcvoip] {e}")

        # No need to also set rfcvoip.DEBUG = True: that flag only gated the
        # *original* print()-based debug() function. Our replacement always
        # calls log.debug() unconditionally — Python's own logging level
        # (DEBUG=file-only, INFO+=console, see _setup_logging() in
        # allstar_discord_bot.py) is what actually controls visibility now.
        rfcvoip.debug          = _bridged_debug
        _rfcvoip_sip.debug     = _bridged_debug
        _rfcvoip_rtp.debug     = _bridged_debug
        _rfcvoip_voip.debug    = _bridged_debug
        _rfcvoip_debug_bridged = True
        log.debug(f"rfcvoip internal debug() bridged into k2br.sip logger (verbose={verbose})")
    except Exception:
        log.debug("Could not bridge rfcvoip debug() — its internals will stay silent", exc_info=True)


class ConnectionState(Enum):
    IDLE         = auto()
    CONNECTING   = auto()
    CONNECTED    = auto()
    RECONNECTING = auto()
    STOPPED      = auto()


# ─────────────────────────────────────────────────────────────────────────────
# Audio client
# ─────────────────────────────────────────────────────────────────────────────

class RepeaterAudioClient:
    """
    Manages a SIP call to one AllStar/HamVOIP node and buffers the audio.

    A background thread:
      1. Imports rfcvoip (lazy — avoids hard import failure if not installed)
      2. Registers the bot as a SIP softphone with Asterisk
      3. Calls the configured extension (e.g. the node number)
      4. Reads decoded RTP audio, converts it to discord.py PCM, buffers it
      5. Auto-reconnects with back-off if the call drops

    discord.py reads frames from the buffer via RepeaterAudioSource.read() at
    exactly 50 fps.  Frames are returned from read_frame(); silence is returned
    when the buffer is empty (during connect/reconnect windows).

    Health monitoring
    ------------------
    state / state_duration expose the connection state machine (IDLE,
    CONNECTING, CONNECTED, RECONNECTING, STOPPED) and how long it's been in
    that state, so a caller can alert if a client is stuck RECONNECTING.

    Voice-activity detection
    -------------------------
    If on_transmission is given, a simple energy-threshold VAD watches the RX
    stream and calls on_transmission(duration_seconds) once per completed
    transmission (i.e. when the signal drops back below threshold), from the
    background thread. Callers on the asyncio event loop should marshal back
    with asyncio.run_coroutine_threadsafe — see how _after_play does this for
    playback-end callbacks in allstar_discord_bot.py.

    TX hook
    -------
    send_frame(pcm_48k_stereo) is a documented stub.  Future implementation:
      - downsample 48kHz stereo → 8kHz mono  (audioop.ratecv + audioop.tomono)
      - encode to G.711 μ-law               (audioop.lin2ulaw)
      - apply VOX threshold + callsign check
      - send via rfcvoip RTP                (call.write_audio)
    """

    def __init__(
        self,
        host:              str,
        port:              int,
        username:          str,
        password:          str,
        extension:         str,
        local_ip:          str = "",
        on_transmission:   Optional[Callable[[float], None]] = None,
        on_speaking_change: Optional[Callable[[bool], None]] = None,
        on_drained:        Optional[Callable[[], None]] = None,
        vad_rms_threshold: int = 400,
        vad_hangover_seconds: float = 1.5,
        local_sip_port: int = 5060,
        ptt_key_dtmf:   str = "*99",
        ptt_unkey_dtmf: str = "#",
        rtp_port_low:   int = 10000,
        rtp_port_high:  int = 20000,
        sip_debug_verbose: bool = False,
        record_transmissions: bool = False,
        max_recording_seconds: float = 300.0,
    ) -> None:
        self.host      = host
        self.port      = port
        self.username  = username
        self.password  = password
        self.extension = extension
        self.local_ip  = local_ip or _detect_local_ip(host)
        # Every concurrently-running client on one machine needs its OWN
        # local SIP port and, ideally, its own RTP port range — two clients
        # binding the same local port is an immediate OSError 10048/EADDRINUSE
        # connect loop (seen the moment always-on monitors made two clients
        # exist at once). The application layer (_ensure_monitors) assigns
        # unique values per repeater.
        self.local_sip_port = int(local_sip_port)
        # app_rpt phone-mode PTT convention: a SIP call into Rpt(node|P)
        # does NOT transmit caller audio until PTT is asserted in-band with
        # DTMF *99; # unkeys. Sent as RFC2833 telephone-events on the
        # negotiated event payload. Empty string = never send (for dialplans
        # that key some other way). NOTE: |D (dumb phone mode) is NOT an
        # alternative here — it keys the transmitter continuously for the
        # whole call, which with always-on monitor calls means keying the
        # repeater 24/7. Use |P.
        self.ptt_key_dtmf   = str(ptt_key_dtmf)
        self.ptt_unkey_dtmf = str(ptt_unkey_dtmf)
        self.rtp_port_low   = int(rtp_port_low)
        self.rtp_port_high  = int(rtp_port_high)
        self.sip_debug_verbose = sip_debug_verbose
        self.record_transmissions  = record_transmissions
        self.max_recording_seconds = max_recording_seconds
        self._recording_frames: list[bytes] = []

        self._state       = ConnectionState.IDLE
        self._state_since = time.time()
        self._running     = False
        self._thread: Optional[threading.Thread] = None

        self._buffer: deque[bytes] = deque(maxlen=BUFFER_MAXFRAMES)
        self._resample_state = None   # maintained across frames for smooth audio — RX direction

        # VAD state — see _update_vad()
        self._on_transmission    = on_transmission
        self._on_speaking_change = on_speaking_change
        # Set on the speaking→False VAD edge instead of pausing playback
        # immediately: the VAD runs at RX time, but playback lags behind by
        # the buffer depth, so pausing right away strands (and eventually
        # evicts) the still-buffered tail of the transmission. read_frame()
        # consumes this flag once the buffer actually drains and fires
        # on_drained, which is where the caller should do vc.pause().
        # Single-bool read/write is atomic under the GIL (same reasoning as
        # _call below) even though it's set on the SIP thread and consumed
        # on discord.py's player thread.
        self._pause_pending      = False
        self._on_drained         = on_drained
        self._vad_rms_threshold  = vad_rms_threshold
        self._vad_hangover_seconds = vad_hangover_seconds
        self._vad_hangover_frames  = max(1, round(vad_hangover_seconds * 1000 / FRAME_MS))
        self._voice_active       = False
        self._silence_run        = 0
        self._activity_start_ts: Optional[float] = None

        # TX state — see send_frame(). _call is set once the SIP call is
        # answered (_connect_and_stream) and cleared when it ends, so
        # send_frame() can tell whether there's actually anything to send to.
        # Read/write of a single reference is atomic under the GIL, same
        # reasoning as _buffer above; send_frame() may be called from a
        # different thread (e.g. Discord voice-receive) than the one running
        # _connect_and_stream(), so no other synchronization is added here.
        self._call = None
        self._tx_resample_state = None   # separate from _resample_state — TX direction
        self._tx_frames_sent = 0          # DEBUG visibility only — see send_frame()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin connecting in the background. Returns immediately."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._run,
            name=f"sip-{self.extension}",
            daemon=True,
        )
        self._thread.start()
        log.info(
            f"SIP audio starting → {self.host}:{self.port}  "
            f"ext={self.extension}  local_ip={self.local_ip}  "
            f"platform={platform.system()}"
        )

    def stop(self) -> None:
        """
        Signal the background thread to hang up and exit. Returns
        immediately — the actual SIP teardown (BYE, deregister) happens on
        the worker thread. At process shutdown, callers MUST follow up with
        wait_stopped(); otherwise the interpreter can exit and kill the
        daemon worker mid-teardown, leaving a zombie call on Asterisk
        (observed live: with two monitors, whichever thread lost the race
        against process exit never sent its BYE).
        """
        self._running = False
        self._set_state(ConnectionState.STOPPED)
        log.info(f"SIP audio stopping [{self.extension}]")

    def wait_stopped(self, timeout: float = 6.0) -> bool:
        """
        Block until the worker thread has finished its teardown (BYE sent,
        phone stopped) or the timeout elapses. Returns True if the thread
        is fully down. Call stop() first; safe to call if never started.
        """
        t = self._thread
        if t is None or not t.is_alive():
            return True
        t.join(timeout)
        return not t.is_alive()

    def read_frame(self) -> bytes:
        """
        Return one 20ms PCM frame (48kHz stereo s16le = 3840 bytes).
        Returns silence if the buffer is empty.
        Thread-safe; called by discord.py's audio player thread at 50 fps.

        When a pause is pending (VAD declared end-of-transmission), the
        pause is deferred until the buffer has fully drained so the tail of
        the transmission — plus the natural squelch tail / courtesy tone
        inside the VAD hangover window — actually plays out instead of being
        stranded in the buffer. on_drained fires exactly once per pending
        pause, from this (player) thread.
        """
        try:
            return self._buffer.popleft()
        except IndexError:
            if self._pause_pending:
                self._pause_pending = False
                if self._on_drained is not None:
                    try:
                        self._on_drained()
                    except Exception:
                        log.exception(f"on_drained callback failed [{self.extension}]")
            return SILENCE

    def flush_buffer(self) -> None:
        """
        Discard all buffered RX audio. Called just before resuming playback:
        while paused, discord.py stops draining the buffer but Asterisk keeps
        sending RTP (silence included), so the deque fills to maxlen and
        playback would otherwise resume ~2s behind real time — which is also
        exactly the audio that got stranded and evicted at the next pause.
        Flushing on resume keeps playback at real time and the buffer depth
        at jitter level. Also clears any stale pause request from the
        previous transmission so a leftover flag can't pause the new one.
        """
        self._buffer.clear()
        self._pause_pending = False

    def key_ptt(self) -> bool:
        """
        Assert PTT on the repeater (app_rpt phone mode) by sending the
        configured DTMF sequence (default *99) as RFC2833 telephone-events.
        Must be called when a transmission starts — audio frames sent
        without PTT asserted are silently discarded by app_rpt. Returns
        True if the DTMF was queued onto the RTP stream.
        """
        return self._send_ptt_dtmf(self.ptt_key_dtmf, "key")

    def unkey_ptt(self) -> bool:
        """Release PTT (default DTMF #). Call on every TX release path."""
        return self._send_ptt_dtmf(self.ptt_unkey_dtmf, "unkey")

    def _send_ptt_dtmf(self, digits: str, action: str) -> bool:
        if not digits:
            return False
        call = self._call
        if call is None:
            log.debug(f"SIP [{self.extension}]: PTT {action} skipped — no active call")
            return False
        try:
            ok = bool(call.send_dtmf(digits))
        except Exception:
            log.warning(f"SIP [{self.extension}]: PTT {action} DTMF {digits!r} raised", exc_info=True)
            return False
        if ok:
            log.info(f"SIP [{self.extension}]: PTT {action} — DTMF {digits!r} sent")
        else:
            log.warning(f"SIP [{self.extension}]: PTT {action} — DTMF {digits!r} NOT sent (no RTP client?)")
        return ok

    def send_frame(self, pcm_48k_stereo: bytes) -> None:
        """
        Send a 20ms PCM frame (48kHz stereo s16le = 3840 bytes) to the repeater.

        No-op if the call isn't currently CONNECTED — callers don't need to
        check state themselves before calling this on every frame.

        Downsamples 48kHz stereo → 8kHz mono the same way _decode() goes the
        other direction, then hands it to rfcvoip's call.write_audio(), which
        encodes to the negotiated codec (PCMU) itself — no manual
        audioop.lin2ulaw step needed, same reasoning as the RX side.
        """
        call = self._call
        if call is None or self._state != ConnectionState.CONNECTED:
            return
        try:
            mono_48k = audioop.tomono(pcm_48k_stereo, 2, 0.5, 0.5)
            pcm_8k_mono, self._tx_resample_state = audioop.ratecv(
                mono_48k, 2, 1,
                DISCORD_SAMPLE_RATE, RTP_SAMPLE_RATE,
                self._tx_resample_state,
            )
            if self._tx_frames_sent == 0:
                log.debug(f"SIP [{self.extension}]: first TX audio frame sent")
            self._tx_frames_sent += 1
            call.write_audio(pcm_8k_mono)
        except Exception:
            log.exception(f"send_frame failed [{self.extension}]")

    @property
    def voice_active(self) -> bool:
        """True while the VAD currently considers a transmission in progress —
        used by playback code to resume immediately when attaching to a
        monitor that's mid-transmission (e.g. right after a preset switch)."""
        return self._voice_active

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def state_duration(self) -> float:
        """Seconds since the connection state last changed."""
        return time.time() - self._state_since

    def _set_state(self, new_state: ConnectionState) -> None:
        """Update state and reset the state_duration clock in one place."""
        old_state = self._state
        old_duration = self.state_duration
        self._state       = new_state
        self._state_since = time.time()
        if old_state != new_state:
            log.debug(
                f"SIP [{self.extension}]: state {old_state.name} → {new_state.name} "
                f"(was {old_state.name} for {old_duration:.2f}s)"
            )

    # ── Background thread ─────────────────────────────────────────────────────

    def _run(self) -> None:
        """Connect, stream, reconnect on failure with exponential back-off."""
        backoff = 2.0
        while self._running:
            try:
                self._set_state(ConnectionState.CONNECTING)
                self._resample_state    = None   # reset on each (re)connect
                self._tx_resample_state = None    # same, but for the TX direction
                self._tx_frames_sent    = 0        # reset the debug counter too
                self._voice_active      = False  # ditto — don't carry a stale
                self._silence_run       = 0       # "in progress" transmission
                self._activity_start_ts = None    # across a reconnect
                self._connect_and_stream()
                backoff = 2.0                        # reset back-off after clean exit
            except Exception as exc:
                if not self._running:
                    break
                log.warning(
                    f"SIP [{self.extension}]: {exc!r} — "
                    f"reconnecting in {backoff:.0f}s",
                    exc_info=True,
                )
                self._set_state(ConnectionState.RECONNECTING)
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)  # cap at 60s

    def _await_registration(self, phone) -> bool:
        """
        Block until the phone reports REGISTERED, or fail fast.

        Returns True to proceed with the INVITE, False if the caller should
        abandon this attempt (registration failed, or stop() was requested).

        Polls VoIPPhone.get_status() rather than sleeping a fixed interval:
        a healthy registration typically confirms in well under a second,
        and a broken one (bad credentials, unreachable host) reports FAILED
        instead of being masked until the INVITE fails downstream. The
        timeout is a backstop for a peer that never answers at all — it is
        deliberately not configurable, since the poll now adapts on its own.

        Status is compared by NAME rather than by importing rfcvoip's
        PhoneStatus enum: the same defensive stance as everywhere else we
        touch this library's internals — if its module layout shifts, we
        degrade to placing the call rather than refusing to connect.
        """
        deadline = time.monotonic() + REGISTRATION_TIMEOUT_SECONDS
        while self._running and time.monotonic() < deadline:
            try:
                status = getattr(phone.get_status(), "name", "")
            except Exception:
                log.debug(
                    f"SIP [{self.extension}]: get_status() unavailable — "
                    f"proceeding without registration confirmation",
                    exc_info=True,
                )
                return True   # never let an rfcvoip API change block calling
            if status == "REGISTERED":
                waited = REGISTRATION_TIMEOUT_SECONDS - (deadline - time.monotonic())
                log.debug(
                    f"SIP [{self.extension}]: registered in {waited:.2f}s — proceeding to call"
                )
                return True
            if status == "FAILED":
                log.warning(
                    f"SIP [{self.extension}]: registration FAILED as "
                    f"'{self.username}' on {self.host}:{self.port} — check the "
                    f"credentials in config.yaml and that this peer exists in "
                    f"sip_custom.conf"
                )
                return False
            time.sleep(REGISTRATION_POLL_SECONDS)

        if self._running:
            log.warning(
                f"SIP [{self.extension}]: no registration confirmation within "
                f"{REGISTRATION_TIMEOUT_SECONDS}s (last status={status or '?'}) — retrying"
            )
        return False

    def _connect_and_stream(self) -> None:
        """
        Register, call, and stream audio until the call ends or stop() is called.
        Raises on any fatal error so _run() can log and retry.
        """
        log.debug(f"SIP [{self.extension}]: _connect_and_stream() starting")

        # Lazy import: bot still starts if rfcvoip isn't installed
        try:
            from rfcvoip.VoIP import VoIPPhone, CallState
        except ImportError:
            log.error(
                "rfcvoip is not installed — SIP audio unavailable.\n"
                "Install it with:  pip install rfcvoip"
            )
            time.sleep(60)
            return
        _bridge_rfcvoip_debug(self.sip_debug_verbose)

        # Pin the *public* audio format to 16-bit signed PCM @ 8kHz mono.
        # rfcvoip decodes the negotiated codec (PCMU) into this format for us
        # before handing it back via read_audio() — see the module docstring.
        log.debug(
            f"SIP [{self.extension}]: constructing VoIPPhone "
            f"(server={self.host}:{self.port}, user={self.username!r}, "
            f"myIP={self.local_ip}, localSipPort={self.local_sip_port}, "
            f"rtp={self.rtp_port_low}-{self.rtp_port_high}, "
            f"audio=16-bit/{RTP_SAMPLE_RATE}Hz/mono)"
        )
        phone = VoIPPhone(
            self.host,
            self.port,
            self.username,
            self.password,
            myIP=self.local_ip,
            sipPort=self.local_sip_port,
            rtpPortLow=self.rtp_port_low,
            rtpPortHigh=self.rtp_port_high,
            audio_bit_depth=16,
            audio_sample_rate=RTP_SAMPLE_RATE,
            audio_channels=1,
        )
        # rfcvoip generates Call-IDs as sha256(counter) with the counter
        # starting at 1 in every process — so every restart replays the
        # exact same Call-ID sequence, and with local SIP ports now pinned
        # per repeater the full Call-ID (hash@ip:port) is byte-identical
        # across runs. If Asterisk still holds a zombie dialog from an
        # unclean shutdown, the new INVITE collides with it and gets
        # remote-BYE'd within a second of answering (observed live).
        # Seeding the counters randomly per phone instance makes every
        # run's identifiers unique, killing that whole failure class.
        try:
            seed = random.SystemRandom().randint(1, 2**31 - 1)
            phone.sip.callID.x = seed
            phone.sip.sessID.x = (seed % 900000) + 1
            log.debug(f"SIP [{self.extension}]: call-id counter seeded at {seed}")
        except Exception:
            log.debug(
                f"SIP [{self.extension}]: could not seed SIP counters "
                f"(rfcvoip internals changed?) — continuing with defaults",
                exc_info=True,
            )

        phone.start()
        # phone.start() only SPAWNS the REGISTER exchange and returns
        # immediately. Earlier versions then slept a fixed
        # registration_wait_seconds and hoped — a blind wait that was both
        # too slow (paid in full on every reconnect even when the 200 OK
        # came back in 100ms) and too weak (a genuinely failed registration
        # was masked until the INVITE failed downstream).
        #
        # rfcvoip does expose the real thing: VoIPPhone.get_status()
        # returns a PhoneStatus, and SIP.register() sets REGISTERED on a
        # successful response / FAILED after too many failures. Poll that
        # instead: proceed the moment registration is confirmed, and fail
        # fast with a clear reason when it won't succeed.
        log.info(f"SIP registration initiated as '{self.username}' on {self.host}:{self.port}")
        if not self._await_registration(phone):
            return   # outer loop applies its normal reconnect backoff

        if not self._running:
            log.debug(f"SIP [{self.extension}]: stop() requested during registration wait — aborting")
            phone.stop()
            return

        call = phone.call(self.extension)
        log.info(f"SIP calling extension {self.extension!r}…")
        log.debug(f"SIP [{self.extension}]: initial call.state={call.state!r}")

        # Wait up to 15 s for Asterisk to answer
        deadline    = time.time() + 15.0
        last_logged_state = call.state
        while time.time() < deadline:
            if not self._running:
                log.debug(f"SIP [{self.extension}]: stop() requested while waiting for answer — hanging up")
                try:
                    call.hangup()
                except Exception:
                    log.debug(f"SIP [{self.extension}]: call.hangup() during abort raised", exc_info=True)
                phone.stop()
                return
            if call.state != last_logged_state:
                log.debug(f"SIP [{self.extension}]: call.state {last_logged_state!r} → {call.state!r}")
                last_logged_state = call.state
            if call.state == CallState.ANSWERED:
                break
            time.sleep(0.1)
        else:
            raise TimeoutError(
                f"Call to extension {self.extension!r} not answered within 15 s "
                f"(final state: {call.state})"
            )

        self._set_state(ConnectionState.CONNECTED)
        self._call = call   # send_frame() checks this; set only once answered
        log.info(f"SIP call answered — streaming node {self.extension}")

        # ── RX loop ──────────────────────────────────────────────────────────
        # Prefer the phone's own frame-size calculation over our hardcoded
        # constant — it accounts for whatever public format was negotiated,
        # per rfcvoip's docs (relevant if this ever runs a wideband codec).
        frame_bytes = getattr(call, "audio_frame_size", lambda: RTP_FRAME_BYTES)()
        log.debug(f"SIP [{self.extension}]: RX loop starting, frame_bytes={frame_bytes}")
        frames_received = 0
        connected_at     = time.time()
        try:
            while self._running and call.state == CallState.ANSWERED:
                pcm_8k_mono = call.read_audio(frame_bytes, blocking=True)
                if pcm_8k_mono:
                    if frames_received == 0:
                        log.debug(f"SIP [{self.extension}]: first RX audio frame received")
                    frames_received += 1
                    if self._on_transmission is not None or self._on_speaking_change is not None:
                        self._update_vad(pcm_8k_mono)
                    self._buffer.append(self._decode(pcm_8k_mono))
        finally:
            connected_secs = time.time() - connected_at
            log.info(
                f"SIP call ended [{self.extension}] — was connected {connected_secs:.1f}s, "
                f"received {frames_received} audio frames "
                f"(final call.state={call.state!r}, self._running={self._running})"
            )
            self._call = None   # stop send_frame() from sending to a dead call
            self._set_state(ConnectionState.IDLE)
            try:
                call.hangup()
            except Exception:
                log.debug(f"SIP [{self.extension}]: call.hangup() during cleanup raised", exc_info=True)
            try:
                phone.stop()
            except Exception:
                log.debug(f"SIP [{self.extension}]: phone.stop() during cleanup raised", exc_info=True)
            # phone.stop() is asynchronous under the hood — its register
            # thread(s) keep running briefly (visible as trailing REGISTER
            # 503s) and the socket teardown can race a fresh VoIPPhone
            # constructed immediately after, which then hits OSError 10038
            # ("not a socket") on its first send. Give the corpse a moment
            # to finish dying before the outer loop rebuilds.
            if self._running:
                log.debug(f"SIP [{self.extension}]: settling {TEARDOWN_SETTLE_SECONDS}s before reconnect")
                time.sleep(TEARDOWN_SETTLE_SECONDS)

    # ── Codec ─────────────────────────────────────────────────────────────────

    def _decode(self, pcm_8k_mono: bytes) -> bytes:
        """
        PCM s16le (8kHz mono) → PCM s16le (48kHz stereo).

        rfcvoip already decodes the negotiated codec (PCMU) into 16-bit
        linear PCM for us — see VoIPPhone(audio_bit_depth=16, ...) in
        _connect_and_stream() — so no manual G.711 μ-law conversion is
        needed here anymore.

        The audioop.ratecv state is carried across calls so resampling is
        continuous — without this, you'd hear a click every 20ms at each
        frame boundary.
        """
        # 8kHz → 48kHz  (stateful for continuous resampling)
        pcm_48k_mono, self._resample_state = audioop.ratecv(
            pcm_8k_mono, 2, 1,
            RTP_SAMPLE_RATE, DISCORD_SAMPLE_RATE,
            self._resample_state,
        )

        # mono → stereo
        return audioop.tostereo(pcm_48k_mono, 2, 1, 1)

    def _update_vad(self, pcm_8k_mono: bytes) -> None:
        """
        Simple energy-threshold VAD with hysteresis, driving two independent
        callbacks off the same underlying state machine:

          on_speaking_change(active: bool) — fires immediately on every edge
          (goes True the instant energy crosses threshold, goes False after
          self._vad_hangover_frames of sustained silence). No minimum-duration
          filter — used to drive Discord voice_client.pause()/resume() so
          the "speaking" indicator reflects real repeater activity instead
          of being permanently lit for the whole streaming session (which is
          what happens if the bot just continuously feeds packets, silence
          included, per Discord's voice protocol — see allstar_discord_bot.py
          for the pause()/resume() wiring).

          on_transmission(duration: float, recording: Optional[bytes]) —
          fires once per completed transmission, filtered to only those
          >= VAD_MIN_TX_SECONDS. Used for activity-channel logging, where a
          sub-second noise blip isn't worth a log line but should still
          make the indicator blink. `recording` is a WAV-encoded bytes blob
          of the transmission's audio (8kHz mono 16-bit, matching the RX
          format directly — no re-encoding) if record_transmissions is
          enabled, else always None.

        Runs on the background thread — callbacks are responsible for their
        own thread-safety (voice_client.pause()/resume() are themselves
        thread-safe; see the comment where they're wired up).

        audioop.rms() on 16-bit PCM is cheap (<<1ms for a 320-byte frame),
        so this runs unconditionally on every RX frame when either callback
        is registered — no separate polling loop needed.
        """
        rms = audioop.rms(pcm_8k_mono, 2)

        if rms >= self._vad_rms_threshold:
            self._silence_run = 0
            if not self._voice_active:
                self._voice_active      = True
                self._activity_start_ts = time.time()
                if self.record_transmissions:
                    self._recording_frames = []
                if self._on_speaking_change is not None:
                    try:
                        self._on_speaking_change(True)
                    except Exception:
                        log.exception(f"on_speaking_change callback failed [{self.extension}]")
            if self.record_transmissions:
                self._append_recording_frame(pcm_8k_mono)
            return

        if not self._voice_active:
            return   # already silent, nothing to hang over

        if self.record_transmissions:
            self._append_recording_frame(pcm_8k_mono)   # keep the natural
                                                            # trailing bit
                                                            # through the
                                                            # hangover window

        self._silence_run += 1
        if self._silence_run < self._vad_hangover_frames:
            return   # still within the hangover window — could be a word gap

        # Sustained silence — the transmission has ended. Subtract the
        # hangover window itself (a known, fixed quantity) so the reported
        # duration reflects actual voice activity, not voice + the trailing
        # silence we waited through before declaring it over.
        duration = (time.time() - self._activity_start_ts) - self._vad_hangover_seconds
        self._voice_active      = False
        self._activity_start_ts = None
        self._silence_run       = 0
        # Defer the actual playback pause to read_frame()/on_drained — see
        # the _pause_pending comment in __init__. The on_speaking_change
        # False edge still fires below for any state the caller keeps, but
        # callers should NOT vc.pause() from it directly.
        self._pause_pending = True
        if self._on_speaking_change is not None:
            try:
                self._on_speaking_change(False)
            except Exception:
                log.exception(f"on_speaking_change callback failed [{self.extension}]")
        if duration >= VAD_MIN_TX_SECONDS and self._on_transmission is not None:
            wav_bytes = self._encode_recording_wav() if self.record_transmissions else None
            try:
                self._on_transmission(duration, wav_bytes)
            except Exception:
                log.exception(f"on_transmission callback failed [{self.extension}]")

    def _append_recording_frame(self, frame: bytes) -> None:
        """
        Buffer one 8kHz mono PCM frame for the in-progress transmission
        recording. Capped at max_recording_seconds worth of frames — once
        hit, further frames are silently dropped (the reported duration and
        activity message are unaffected; only the attached recording gets
        truncated) rather than letting an unusually long transmission (or a
        stuck-open carrier) grow the buffer and the eventual WAV file
        without bound.
        """
        max_frames = int(self.max_recording_seconds * 1000 / FRAME_MS)
        if len(self._recording_frames) < max_frames:
            self._recording_frames.append(frame)

    def _encode_recording_wav(self) -> Optional[bytes]:
        """
        Encode the buffered frames for the just-completed transmission into
        a WAV file (in memory, no temp files). Returns None if nothing was
        actually buffered (e.g. recording was just enabled mid-transmission).
        """
        if not self._recording_frames:
            return None
        buf = io.BytesIO()
        try:
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)   # 16-bit
                wf.setframerate(RTP_SAMPLE_RATE)
                wf.writeframes(b"".join(self._recording_frames))
            return buf.getvalue()
        except Exception:
            log.exception(f"Failed to encode transmission recording [{self.extension}]")
            return None


# ─────────────────────────────────────────────────────────────────────────────
# discord.py AudioSource wrapper
# ─────────────────────────────────────────────────────────────────────────────

class RepeaterAudioSource(discord.AudioSource):
    """
    discord.py AudioSource backed by a RepeaterAudioClient.
    Zero external runtime dependencies — no external media server or
    transcoding binary required.

    discord.py calls read() every 20ms in its audio player thread.
    We return raw PCM; discord.py handles Opus encoding and UDP transmission.
    Silence is returned transparently during SIP connect/reconnect windows.
    """

    def __init__(self, client: RepeaterAudioClient, owns_client: bool = True) -> None:
        """
        owns_client=True (legacy): the source starts the client and stops it
        in cleanup() — client lifetime == playback lifetime.
        owns_client=False (persistent-monitor model): the client is a
        long-lived, always-on SIP monitor owned by the application; the
        source is just a playback view onto it. cleanup() leaves the client
        running so recording/activity detection continues when nothing (or
        something else) is being played to Discord. start() is safe to call
        either way — it's a no-op on an already-running client.
        """
        self._client = client
        self._owns_client = owns_client
        client.start()

    def read(self) -> bytes:
        """
        Return one 20ms PCM frame (3840 bytes: 48kHz · stereo · s16le).
        Returns silence if no audio is buffered yet.
        """
        return self._client.read_frame()

    def is_opus(self) -> bool:
        return False   # we return PCM; discord.py encodes to Opus

    def cleanup(self) -> None:
        if self._owns_client:
            self._client.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _detect_local_ip(target: str) -> str:
    """
    Return the local LAN IP that can route to `target`.
    Uses a UDP connect (no packets sent) to ask the kernel which interface
    it would use, then reads the source address.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target, 5060))
            return s.getsockname()[0]
    except Exception:
        log.warning("Could not auto-detect local IP; defaulting to 0.0.0.0")
        return "0.0.0.0"
