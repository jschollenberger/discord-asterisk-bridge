"""
Media-liveness watchdog: detect a wedged RX audio pipeline and force a reconnect.

rfcvoip's read_audio(blocking=True) spins forever if inbound RTP stops (the
2026-08-08 freeze: both repeaters relayed nothing for ~7h while SIP heartbeats
kept logging OK). A monitor now records the time of its last RX frame, and the
bot forces a reconnect when a CONNECTED call goes silent. These exercise the
pure decision logic and the client hooks without threads or a live SIP call.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import repeater_audio as ra
from repeater_audio import ConnectionState


# ── _media_stall_action (pure decision) ─────────────────────────────────────

def test_action_ok_when_not_connected(bot_module):
    # since_rx is None whenever the call isn't CONNECTED.
    assert bot_module._media_stall_action(None, None, 45, 30) == "ok"


def test_action_ok_when_recently_received(bot_module):
    assert bot_module._media_stall_action(10.0, None, 45, 30) == "ok"


def test_action_force_when_silent_and_never_forced(bot_module):
    assert bot_module._media_stall_action(60.0, None, 45, 30) == "force"


def test_action_wait_during_reconnect_cooldown(bot_module):
    # Silent, but we forced a reconnect 5s ago — give it time to come back.
    assert bot_module._media_stall_action(60.0, 5.0, 45, 30) == "wait"


def test_action_force_again_after_cooldown(bot_module):
    # Still silent 40s after the last force (> cooldown) — force again (escalates).
    assert bot_module._media_stall_action(60.0, 40.0, 45, 30) == "force"


# ── RepeaterAudioClient liveness hooks ──────────────────────────────────────

def _client(*, state=ConnectionState.CONNECTED, last_rx=None, last_force=0.0):
    """A client instance without __init__ (no SIP, no threads)."""
    c = ra.RepeaterAudioClient.__new__(ra.RepeaterAudioClient)
    c.extension = "50420"
    c._state = state
    c._last_rx_monotonic = time.monotonic() if last_rx is None else last_rx
    c._last_force_monotonic = last_force
    c._call = None
    c._phone = None
    return c


def test_seconds_since_rx_none_when_not_connected():
    assert _client(state=ConnectionState.RECONNECTING).seconds_since_rx() is None


def test_seconds_since_rx_none_before_first_connect():
    # _last_rx_monotonic == 0.0 means we've never armed liveness yet.
    assert _client(state=ConnectionState.CONNECTED, last_rx=0.0).seconds_since_rx() is None


def test_seconds_since_rx_measures_gap():
    c = _client(state=ConnectionState.CONNECTED, last_rx=time.monotonic() - 5.0)
    gap = c.seconds_since_rx()
    assert gap is not None and 4.0 < gap < 30.0


def test_seconds_since_force_none_when_never():
    assert _client(last_force=0.0).seconds_since_force() is None


def test_force_reconnect_closes_call_then_phone():
    order = []
    c = _client()
    c._call = SimpleNamespace(hangup=lambda: order.append("hangup"))
    c._phone = SimpleNamespace(stop=lambda: order.append("stop"))
    c.force_reconnect("no RX audio")
    assert order == ["hangup", "stop"]        # unblocks the wedged read
    assert c.seconds_since_force() is not None  # timestamp recorded for the cooldown


def test_force_reconnect_survives_missing_call_and_phone():
    c = _client()                 # _call and _phone are None (never answered)
    c.force_reconnect("no RX audio")   # must not raise
    assert c.seconds_since_force() is not None


def test_force_reconnect_swallows_close_errors():
    def boom():
        raise RuntimeError("already closed")

    c = _client()
    c._call = SimpleNamespace(hangup=boom)
    c._phone = SimpleNamespace(stop=boom)
    c.force_reconnect("no RX audio")   # a raising hangup/stop must not escape
