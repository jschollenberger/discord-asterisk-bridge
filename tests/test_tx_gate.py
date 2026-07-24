"""TX gate (_tx_try_relay): the authorization + lock state machine that decides
who gets to key the physical transmitter.

This is safety-relevant: only a configured operator may transmit, only one holds
a repeater's lock at a time, a lock is never acquired while the SIP link is down,
and a mid-transmission connection drop releases the lock and unkeys PTT. Called
at packet rate from a background thread, so it must stay pure dict work.
"""
from __future__ import annotations

import types


class _FakeClient:
    """Stand-in for a repeater's RepeaterAudioClient monitor."""
    def __init__(self, state: str = "CONNECTED"):
        self.state = types.SimpleNamespace(name=state)
        self.sent: list[bytes] = []
        self.keyed = 0
        self.unkeyed = 0

    def key_ptt(self): self.keyed += 1
    def unkey_ptt(self): self.unkeyed += 1
    def send_frame(self, pcm): self.sent.append(pcm)


def _op(callsign: str):
    return types.SimpleNamespace(callsign=callsign)


def _wire(m, monkeypatch, operators: dict[int, str], state: str = "CONNECTED"):
    """Register a fake monitor for 'vhf' and a fixed operator table; disable the
    event-loop hop so the packet-path coroutine scheduling is skipped."""
    monkeypatch.setattr(m, "_loop", None)
    monkeypatch.setattr(
        m.cfg, "tx_operator_by_discord_id",
        lambda uid: _op(operators[uid]) if uid in operators else None,
    )
    fc = _FakeClient(state)
    m._monitor_clients["vhf"] = fc
    return fc


def test_unauthorized_user_is_dropped(bot_module, monkeypatch):
    m = bot_module
    fc = _wire(m, monkeypatch, {})           # nobody authorized
    m._tx_try_relay(1, "vhf", 999, b"x")
    assert fc.sent == [] and fc.keyed == 0
    assert "vhf" not in m._tx_locks


def test_authorized_operator_acquires_lock_keys_and_sends(bot_module, monkeypatch):
    m = bot_module
    fc = _wire(m, monkeypatch, {42: "K2BR"})
    m._tx_try_relay(1, "vhf", 42, b"frame")
    lock = m._tx_locks.get("vhf")
    assert lock is not None
    assert lock.holder_user_id == 42 and lock.callsign == "K2BR"
    assert fc.keyed == 1                      # PTT asserted
    assert fc.sent == [b"frame"]


def test_no_lock_acquired_while_sip_is_down(bot_module, monkeypatch):
    m = bot_module
    fc = _wire(m, monkeypatch, {42: "K2BR"}, state="RECONNECTING")
    m._tx_try_relay(1, "vhf", 42, b"frame")
    assert "vhf" not in m._tx_locks           # no false "keyed up"
    assert fc.keyed == 0 and fc.sent == []


def test_second_speaker_is_dropped_over_the_holder(bot_module, monkeypatch):
    m = bot_module
    fc = _wire(m, monkeypatch, {42: "K2BR", 43: "W1AW"})
    m._tx_try_relay(1, "vhf", 42, b"a")       # 42 acquires
    m._tx_try_relay(1, "vhf", 43, b"b")       # 43 keys over → dropped
    assert m._tx_locks["vhf"].holder_user_id == 42
    assert fc.sent == [b"a"]                   # 43's audio never relayed
    assert fc.keyed == 1                       # keyed once, for 42


def test_holder_refresh_keeps_sending_without_re_keying(bot_module, monkeypatch):
    m = bot_module
    fc = _wire(m, monkeypatch, {42: "K2BR"})
    m._tx_try_relay(1, "vhf", 42, b"a")
    first = m._tx_locks["vhf"].last_packet_at
    m._tx_try_relay(1, "vhf", 42, b"b")
    assert m._tx_locks["vhf"].last_packet_at >= first
    assert fc.sent == [b"a", b"b"]
    assert fc.keyed == 1                        # not re-keyed on continuation


def test_connection_drop_mid_transmission_releases_and_unkeys(bot_module, monkeypatch):
    m = bot_module
    fc = _wire(m, monkeypatch, {42: "K2BR"})
    m._tx_try_relay(1, "vhf", 42, b"a")        # acquire while CONNECTED
    assert "vhf" in m._tx_locks

    fc.state.name = "RECONNECTING"             # SIP drops mid-over
    m._tx_try_relay(1, "vhf", 42, b"b")
    assert "vhf" not in m._tx_locks            # lock released
    assert fc.unkeyed == 1                      # PTT released
    assert fc.sent == [b"a"]                    # 'b' not relayed
