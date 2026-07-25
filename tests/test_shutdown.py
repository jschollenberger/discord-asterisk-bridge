"""Shutdown observability: the operator must actually SEE the shutdown banner
and teardown logs on Ctrl-C.

The bug these guard against wasn't a logic error — the messages were emitted,
but the Rich Live dashboard (a background thread repainting a pinned region
every second) painted over them and scrolled them off-screen during discord.py's
~10–15s voice-disconnect handshake. The fix is an ordering contract: halt the
refresh worker and tear the Live region down BEFORE printing. These tests pin
that contract (which a plain unit test of "was it emitted?" would never catch).
"""
from __future__ import annotations

import threading
import time


class _FakeLive:
    def __init__(self, stop_raises=False):
        self._stop_raises = stop_raises
        self.updates = 0

    def update(self, _renderable):
        self.updates += 1

    def stop(self):
        _events.append("live.stop")
        if self._stop_raises:
            raise RuntimeError("teardown hiccup")


class _FakeConsole:
    def print(self, *_a, **_k):
        _events.append("print")


_events: list[str] = []


def _reset():
    _events.clear()


# ── _begin_shutdown: tear the dashboard down BEFORE printing ──────────────────

def test_begin_shutdown_stops_dashboard_before_printing(bot_module):
    _reset()
    stop = threading.Event()
    bot_module._begin_shutdown(_FakeLive(), stop, _FakeConsole())
    assert stop.is_set()                      # worker signalled to halt
    assert _events == ["live.stop", "print"]  # region removed BEFORE the banner


def test_begin_shutdown_is_idempotent(bot_module):
    _reset()
    stop = threading.Event()
    live = _FakeLive()
    bot_module._begin_shutdown(live, stop, _FakeConsole())
    bot_module._begin_shutdown(live, stop, _FakeConsole())   # SIGINT then finally
    assert _events == ["live.stop", "print"]  # second call is a no-op


def test_begin_shutdown_prints_even_if_live_stop_fails(bot_module):
    # A dashboard-teardown hiccup must never swallow the shutdown banner —
    # that would recreate the "looks like nothing happened" symptom.
    _reset()
    stop = threading.Event()
    bot_module._begin_shutdown(_FakeLive(stop_raises=True), stop, _FakeConsole())
    assert _events == ["live.stop", "print"]
    assert stop.is_set()


# ── _dashboard_worker: paint promptly, stop promptly, no stray repaint ────────

def test_dashboard_worker_paints_then_stops_without_repainting(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module, "build_dashboard", lambda: object())
    live = _FakeLive()
    stop = threading.Event()
    t = threading.Thread(target=bot_module._dashboard_worker, args=(live, stop), daemon=True)
    t.start()
    time.sleep(0.05)
    assert live.updates >= 1          # dashboard shown immediately, not after 1s

    painted = live.updates
    stop.set()                        # request shutdown
    t.join(timeout=2.0)
    assert not t.is_alive()           # woke from the wait at once (not a 1s sleep)
    assert live.updates == painted    # no repaint after stop — nothing to clobber
