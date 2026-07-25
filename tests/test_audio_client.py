"""RepeaterAudioClient unit behavior: buffering, drain-pause, PTT, teardown."""
from __future__ import annotations

import threading
import time

import pytest

import repeater_audio as ra


class _Status:
    """Stand-in for rfcvoip's PhoneStatus — the client compares .name only."""
    def __init__(self, name: str): self.name = name


def _bare_client() -> ra.RepeaterAudioClient:
    """Client instance without running __init__ (no SIP, no threads)."""
    c = ra.RepeaterAudioClient.__new__(ra.RepeaterAudioClient)
    from collections import deque
    c._buffer = deque(maxlen=100)
    c._pause_pending = False
    c._on_drained = None
    c.extension = "50420"
    c.username = "test-vhf"
    c.host = "127.0.0.1"
    c.port = 5060
    c._running = False
    c._call = None
    c._thread = None
    return c


def test_read_frame_returns_silence_when_empty():
    c = _bare_client()
    assert c.read_frame() == ra.SILENCE


def test_drain_then_pause_fires_on_drained_once():
    events = []
    c = _bare_client()
    c._on_drained = lambda: events.append("pause")
    c._buffer.extend([b"a", b"b"])
    c._pause_pending = True
    out = [c.read_frame() for _ in range(4)]
    assert out[:2] == [b"a", b"b"]          # tail plays out first
    assert out[2] == ra.SILENCE and events == ["pause"]
    assert c.read_frame() == ra.SILENCE and events == ["pause"]  # exactly once


def test_ingest_drops_frames_while_pause_pending():
    """Continuous RTP must not refill the playback buffer once VAD has declared
    end-of-transmission. AllStar's phone-mode call streams ~50 fps even in
    silence, so if we kept enqueueing, the buffer would never drain to empty and
    the pause (Discord speaking indicator off) would never fire."""
    c = _bare_client()
    c._resample_state = None
    c._on_transmission = None
    c._on_speaking_change = None
    frame = bytes(ra.RTP_FRAME_BYTES)

    c._ingest_rx_frame(frame)            # not pausing → buffered
    assert len(c._buffer) == 1
    c._pause_pending = True
    for _ in range(10):
        c._ingest_rx_frame(frame)        # pausing → dropped
    assert len(c._buffer) == 1           # unchanged


def test_drain_fires_pause_despite_continuous_rtp():
    """End-to-end guarantee: with RTP still arriving on every frame, a pending
    pause still drains to empty and fires on_drained exactly once."""
    events = []
    c = _bare_client()
    c._resample_state = None
    c._on_transmission = None
    c._on_speaking_change = None
    c._on_drained = lambda: events.append("pause")
    c._buffer.extend([b"a", b"b"])       # buffered tail captured at VAD end
    c._pause_pending = True
    frame = bytes(ra.RTP_FRAME_BYTES)
    for _ in range(4):
        c._ingest_rx_frame(frame)        # continuous RTP — must NOT refill
        c.read_frame()                   # player draining in lockstep
    assert events == ["pause"]           # drained + paused despite the RTP


def test_flush_clears_buffer_and_stale_pause():
    c = _bare_client()
    c._buffer.append(b"x")
    c._pause_pending = True
    c.flush_buffer()
    assert len(c._buffer) == 0 and c._pause_pending is False


def test_non_owning_source_never_stops_client():
    class FakeClient:
        def __init__(self): self.started = 0; self.stopped = 0
        def start(self): self.started += 1
        def stop(self): self.stopped += 1
        def read_frame(self): return b"x"
    fc = FakeClient()
    non_owning = ra.RepeaterAudioSource(fc, owns_client=False)
    non_owning.cleanup()
    assert fc.started == 1 and fc.stopped == 0
    del non_owning              # __del__ → cleanup() again: still must not stop
    assert fc.stopped == 0

    owning = ra.RepeaterAudioSource(fc, owns_client=True)
    owning.cleanup()
    assert fc.stopped == 1      # (held reference — GC hasn't double-cleaned)


def test_ptt_dtmf_routes_to_call_and_is_none_safe():
    c = _bare_client()
    c.ptt_key_dtmf, c.ptt_unkey_dtmf = "*99", "#"
    assert c.key_ptt() is False              # no active call → safe no-op

    class FakeCall:
        def __init__(self): self.sent = []
        def send_dtmf(self, d): self.sent.append(d); return True
    fake = FakeCall()
    c._call = fake
    assert c.key_ptt() and c.unkey_ptt()
    assert fake.sent == ["*99", "#"]


def test_ptt_empty_digits_disable_sending():
    c = _bare_client()
    c.ptt_key_dtmf = ""
    c._call = object()   # would explode if touched
    assert c.key_ptt() is False


def test_wait_stopped_joins_worker():
    c = _bare_client()
    done = threading.Event()
    c._thread = threading.Thread(target=lambda: (time.sleep(0.2), done.set()), daemon=True)
    c._thread.start()
    assert c.wait_stopped(2.0) is True and done.is_set()
    assert c.wait_stopped(0.1) is True       # already down / idempotent


def test_await_registration_returns_on_registered():
    """Polls status instead of sleeping a fixed interval: proceeds as soon as
    the REGISTER 200 OK lands (previously always paid ~1.5s)."""
    c = _bare_client()
    c._running = True

    class Phone:
        def __init__(self): self.calls = 0
        def get_status(self):
            self.calls += 1
            return _Status('REGISTERING') if self.calls < 3 else _Status('REGISTERED')

    start = time.monotonic()
    assert c._await_registration(Phone()) is True
    assert time.monotonic() - start < 1.0      # far below the old blind 1.5s


def test_await_registration_fails_fast_on_failed():
    """A failed registration reports immediately rather than being masked
    until the downstream INVITE fails."""
    c = _bare_client()
    c._running = True

    class Phone:
        def get_status(self): return _Status('FAILED')

    start = time.monotonic()
    assert c._await_registration(Phone()) is False
    assert time.monotonic() - start < 0.5


def test_await_registration_survives_missing_api():
    """If rfcvoip's status API ever changes shape, calling must not be
    blocked — degrade to proceeding rather than refusing to connect."""
    c = _bare_client()
    c._running = True

    class Phone:
        def get_status(self): raise AttributeError("api changed")

    assert c._await_registration(Phone()) is True


def test_await_registration_aborts_when_stopping():
    c = _bare_client()
    c._running = False

    class Phone:
        def get_status(self): return _Status('REGISTERING')

    assert c._await_registration(Phone()) is False


# ── _connect_and_stream teardown: phone.stop() must run on EVERY exit ──────────
# Regression guard: phone.start() binds the local SIP socket and spawns threads.
# rfcvoip binds without SO_REUSEADDR, so a phone that starts but is then
# abandoned without stop() leaks the bound local port — and the next reconnect's
# start() fails to rebind (EADDRINUSE), wedging the monitor. An earlier version
# wrapped only the RX loop in try/finally, so a failed registration or an
# unanswered/failed call returned/raised past it and leaked. The finally now
# spans the whole session.

def _session_client(monkeypatch) -> ra.RepeaterAudioClient:
    """Bare client wired with just enough state to drive _connect_and_stream(),
    with the rfcvoip debug bridge and the post-teardown settle sleep stubbed
    out so the test neither imports SIP internals nor waits on the settle."""
    c = _bare_client()
    c.password        = "secret"
    c.local_ip        = "127.0.0.1"
    c.local_sip_port  = 5060
    c.rtp_port_low    = 10000
    c.rtp_port_high   = 11999
    c.sip_debug_verbose = False
    c._state          = ra.ConnectionState.CONNECTING
    c._state_since    = time.time()
    c._running        = True
    monkeypatch.setattr(ra, "_bridge_rfcvoip_debug", lambda *a, **k: None)
    monkeypatch.setattr(ra, "TEARDOWN_SETTLE_SECONDS", 0.0)
    return c


def test_connect_stops_phone_when_registration_fails(monkeypatch):
    """Started OK but never registered → must still stop the phone."""
    c = _session_client(monkeypatch)
    created = []

    class FakePhone:
        def __init__(self): self.stopped = 0
        def start(self): pass
        def stop(self, *a, **k): self.stopped += 1
        def get_status(self): return _Status("FAILED")

    def _factory(*a, **k):
        p = FakePhone(); created.append(p); return p
    monkeypatch.setattr("rfcvoip.VoIP.VoIPPhone", _factory)

    c._connect_and_stream()   # returns normally on a failed registration
    assert created and created[0].stopped == 1


def test_connect_stops_phone_when_call_setup_raises(monkeypatch):
    """Any failure after a successful start (a raising call(), the answer
    timeout, an RX error) runs the same teardown — the phone is always stopped
    even though the exception propagates to _run() for its reconnect backoff."""
    c = _session_client(monkeypatch)
    created = []

    class FakePhone:
        def __init__(self): self.stopped = 0
        def start(self): pass
        def stop(self, *a, **k): self.stopped += 1
        def get_status(self): return _Status("REGISTERED")
        def call(self, ext): raise RuntimeError("INVITE failed")

    def _factory(*a, **k):
        p = FakePhone(); created.append(p); return p
    monkeypatch.setattr("rfcvoip.VoIP.VoIPPhone", _factory)

    with pytest.raises(RuntimeError):
        c._connect_and_stream()
    assert created and created[0].stopped == 1


def test_connect_skips_hangup_when_call_already_ended(monkeypatch):
    """A remote BYE (e.g. the far-end Asterisk restarting) moves the call to
    ENDED before cleanup. rfcvoip.hangup() raises InvalidStateError on a
    non-ANSWERED call, so we must not call it — that path logged a full,
    alarming traceback for what is a normal drop/reconnect."""
    from rfcvoip.VoIP import CallState
    c = _session_client(monkeypatch)

    class FakeCall:
        def __init__(self): self.state = CallState.ANSWERED; self.hangups = 0
        def read_audio(self, n, blocking=True):
            self.state = CallState.ENDED   # far end ended the call mid-stream
            return b""
        def hangup(self):
            self.hangups += 1
            raise AssertionError("hangup() must not run on an already-ended call")

    fake = FakeCall()

    class FakePhone:
        def start(self): pass
        def stop(self, *a, **k): pass
        def get_status(self): return _Status("REGISTERED")
        def call(self, ext): return fake

    monkeypatch.setattr("rfcvoip.VoIP.VoIPPhone", lambda *a, **k: FakePhone())
    c._connect_and_stream()
    assert fake.hangups == 0


def test_connect_hangs_up_a_still_answered_call(monkeypatch):
    """The counterpart: a call that's still ANSWERED at cleanup (e.g. a local
    stop() mid-stream) IS hung up, so we don't leave a live call dangling."""
    from rfcvoip.VoIP import CallState
    c = _session_client(monkeypatch)

    class FakeCall:
        def __init__(self): self.state = CallState.ANSWERED; self.hangups = 0
        def read_audio(self, n, blocking=True):
            c._running = False             # local stop; call stays ANSWERED
            return b""
        def hangup(self): self.hangups += 1

    fake = FakeCall()

    class FakePhone:
        def start(self): pass
        def stop(self, *a, **k): pass
        def get_status(self): return _Status("REGISTERED")
        def call(self, ext): return fake

    monkeypatch.setattr("rfcvoip.VoIP.VoIPPhone", lambda *a, **k: FakePhone())
    c._connect_and_stream()
    assert fake.hangups == 1
