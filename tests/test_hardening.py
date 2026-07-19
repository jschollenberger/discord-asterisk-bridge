"""voice_recv defensive patches: per-packet decode isolation, CryptoError filter."""
from __future__ import annotations

import logging

import pytest


def test_pop_data_survives_decode_failures(bot_module):
    vr = pytest.importorskip("discord.ext.voice_recv.opus")
    bot_module._harden_voice_recv_decoder()

    class Boom(vr.PacketDecoder):
        def __init__(self):  # no super() — avoid threads/queues
            self.ssrc = 4321
            self.resets = 0
        def reset(self): self.resets += 1
        def _get_next_packet(self, timeout):
            raise RuntimeError("corrupted stream (simulated)")
        def _flag_ready_state(self): pass

    d = Boom()
    assert d.pop_data() is None       # must not raise
    assert d.resets == 1              # decoder state resynced
    assert d.pop_data() is None and d.resets == 2


def test_hardening_is_idempotent(bot_module):
    pytest.importorskip("discord.ext.voice_recv.opus")
    bot_module._harden_voice_recv_decoder()
    from discord.ext.voice_recv.opus import PacketDecoder
    first = PacketDecoder.pop_data
    bot_module._harden_voice_recv_decoder()
    assert PacketDecoder.pop_data is first    # not double-wrapped


def test_cryptoerror_filter_demotes_and_rate_limits(bot_module, caplog):
    lg = logging.getLogger("discord.ext.voice_recv.reader")
    with caplog.at_level(logging.DEBUG, logger="discord.ext.voice_recv.reader"):
        for _ in range(5):
            lg.error("CryptoError decoding packet data")
        lg.error("a genuinely different error")
    crypto = [r for r in caplog.records if "undecryptable" in r.getMessage()]
    passthru = [r for r in caplog.records if "genuinely different" in r.getMessage()]
    assert len(crypto) == 1 and crypto[0].levelno == logging.WARNING
    assert len(passthru) == 1 and passthru[0].levelno == logging.ERROR


def test_sip_heartbeat_aggregator(bot_module, caplog):
    lg = logging.getLogger("k2br.sip")
    hb = [f for f in lg.filters if type(f).__name__ == "_SipHeartbeatAggregator"]
    assert hb, "aggregator filter must be attached to k2br.sip"
    f = hb[0]
    f._counts.clear()
    f._window_start = 0.0   # force the window to be already-elapsed
    with caplog.at_level(logging.DEBUG, logger="k2br.sip"):
        lg.debug("[rfcvoip] Method: OPTIONS")        # counted, elapsed → summary
        lg.debug("[rfcvoip] Status: 200 OK")          # new window → swallowed
        lg.debug("[rfcvoip] New register thread")     # swallowed
        lg.debug("[rfcvoip] Status: 407 Proxy Authentication Required")  # passthrough
        lg.info("SIP call answered — streaming node 50420")              # passthrough
    msgs = [r.getMessage() for r in caplog.records if r.name == "k2br.sip"]
    assert any("SIP heartbeat OK" in m and "1× OPTIONS" in m for m in msgs)
    assert not any(m == "[rfcvoip] Status: 200 OK" for m in msgs)
    assert any("407" in m for m in msgs)
    assert any("call answered" in m for m in msgs)
