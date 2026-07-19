"""Always-on monitor lifecycle: port allocation, idempotency, shutdown join."""
from __future__ import annotations

import threading
import time


def test_port_allocation_unique_and_idempotent(bot_module, cfg, monkeypatch):
    import repeater_audio as ra
    created = []

    class FakeClient:
        def __init__(self, **kw): created.append(kw)
        def start(self): pass

    monkeypatch.setattr(ra, "RepeaterAudioClient", FakeClient)
    bot_module._ensure_monitors()
    ports = [(c["local_sip_port"], c["rtp_port_low"], c["rtp_port_high"]) for c in created]
    assert ports == [(5060, 10000, 11999), (5062, 12000, 13999)]
    assert set(bot_module._monitor_clients) == {"vhf", "uhf"}

    created.clear()
    bot_module._ensure_monitors()
    assert created == []                      # second call creates nothing


def test_explicit_local_sip_port_is_respected(bot_module, cfg, monkeypatch):
    import repeater_audio as ra
    created = []

    class FakeClient:
        def __init__(self, **kw): created.append(kw)
        def start(self): pass

    monkeypatch.setattr(ra, "RepeaterAudioClient", FakeClient)
    saved = cfg.repeater_by_id("vhf").sip_audio.local_sip_port
    try:
        cfg.repeater_by_id("vhf").sip_audio.local_sip_port = 5100
        bot_module._ensure_monitors()
        assert created[0]["local_sip_port"] == 5100
        assert created[1]["local_sip_port"] == 5060   # auto skips nothing it needn't
    finally:
        cfg.repeater_by_id("vhf").sip_audio.local_sip_port = saved


def test_stop_monitors_signals_all_then_joins(bot_module):
    """Mirrors the live VHF-fast/UHF-slow shutdown race: both teardowns must
    complete before _stop_monitors returns, in parallel (max, not sum)."""
    import repeater_audio as ra

    class FakeMonitor:
        def __init__(self, teardown_secs):
            self._running = True
            self.bye_sent = False
            def worker():
                while self._running:
                    time.sleep(0.01)
                time.sleep(teardown_secs)
                self.bye_sent = True
            self._thread = threading.Thread(target=worker, daemon=True)
            self._thread.start()
        def stop(self): self._running = False
        wait_stopped = ra.RepeaterAudioClient.wait_stopped

    fast, slow = FakeMonitor(0.05), FakeMonitor(0.4)
    bot_module._monitor_clients.update({"vhf": fast, "uhf": slow})
    bot_module._stop_monitors()
    # The regression this guards: process exit used to race the daemon
    # workers, so the slow monitor's BYE never went out. Both teardowns must
    # have COMPLETED by the time _stop_monitors returns.
    assert fast.bye_sent and slow.bye_sent
    assert bot_module._monitor_clients == {}


def test_disabled_repeater_gets_no_monitor(bot_module, cfg, monkeypatch):
    import repeater_audio as ra
    created = []

    class FakeClient:
        def __init__(self, **kw): created.append(kw)
        def start(self): pass

    monkeypatch.setattr(ra, "RepeaterAudioClient", FakeClient)
    cfg.repeater_by_id("uhf").enabled = False
    try:
        bot_module._ensure_monitors()
        assert set(bot_module._monitor_clients) == {"vhf"}
        assert len(created) == 1
    finally:
        cfg.repeater_by_id("uhf").enabled = True


def test_disabled_repeater_cannot_be_played(bot_module, cfg):
    cfg.repeater_by_id("uhf").enabled = False
    try:
        with __import__("pytest").raises(bot_module.NoAudioConfiguredError) as exc:
            bot_module._make_source("uhf")
        assert "disabled" in str(exc.value).lower()
    finally:
        cfg.repeater_by_id("uhf").enabled = True
