"""channel_status_task: debounce + clean handling of Discord's voice-status limit.

Discord rate-limits PUT /voice-status hard (429s, and 5xx under load) and the
"on the air" bit flips every over, so the task must (a) cap its own edit rate
per channel and (b) swallow the expected HTTP errors without dumping a full
traceback. Drives the task's coroutine directly with fakes — no real Discord.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def _wire(monkeypatch, m, ch, *, clock):
    """Wire the module globals channel_status_task reads, with a controllable clock."""
    vc = SimpleNamespace(channel=ch, is_connected=lambda: True)
    guild = SimpleNamespace(id=1, voice_client=vc)
    monkeypatch.setattr(m, "bot", SimpleNamespace(guilds=[guild], get_channel=lambda cid: None))
    monkeypatch.setattr(m, "_vc", lambda x: x)
    monkeypatch.setattr(m, "get_state", lambda gid: SimpleNamespace(preset="vhf"))
    monkeypatch.setattr(m, "_monitor_clients", {"vhf": object()})
    monkeypatch.setattr(m, "_channel_status", {})
    monkeypatch.setattr(m, "_channel_status_edit_ts", {})
    monkeypatch.setattr(m, "_channel_status_supported", True)
    monkeypatch.setattr(m.time, "monotonic", lambda: clock["t"])


def test_status_edits_are_debounced(bot_module, monkeypatch):
    m = bot_module
    ch = MagicMock(spec=m.discord.VoiceChannel)   # isinstance(ch, VoiceChannel) is True
    ch.id = 555
    ch.edit = AsyncMock()
    clock = {"t": 1000.0}
    _wire(monkeypatch, m, ch, clock=clock)
    # A status that changes every cycle: only the debounce can gate the edits.
    texts = iter(["A", "B", "C", "D"])
    monkeypatch.setattr(m, "_voice_status_text", lambda rpt, client: next(texts))

    asyncio.run(m.channel_status_task.coro())          # t=1000 → edit "A"
    clock["t"] = 1000.5
    asyncio.run(m.channel_status_task.coro())          # +0.5s → within window → skip
    assert ch.edit.await_count == 1
    clock["t"] = 1000.0 + m._STATUS_MIN_EDIT_INTERVAL + 1
    asyncio.run(m.channel_status_task.coro())          # past the window → edit again
    assert ch.edit.await_count == 2


def test_rate_limit_is_swallowed_without_traceback(bot_module, monkeypatch, caplog):
    m = bot_module
    resp = SimpleNamespace(status=429, reason="Too Many Requests")
    ch = MagicMock(spec=m.discord.VoiceChannel)
    ch.id = 777
    ch.edit = AsyncMock(side_effect=m.discord.HTTPException(resp, "rate limited"))
    clock = {"t": 5000.0}
    _wire(monkeypatch, m, ch, clock=clock)
    monkeypatch.setattr(m, "_voice_status_text", lambda rpt, client: "X")

    with caplog.at_level(logging.DEBUG):
        asyncio.run(m.channel_status_task.coro())      # must not raise

    ch.edit.assert_awaited_once()
    recs = [r for r in caplog.records if "status update deferred" in r.getMessage()]
    assert len(recs) == 1
    assert recs[0].exc_info is None                     # clean line, not a traceback
    assert "HTTP 429" in recs[0].getMessage()
    assert m._channel_status_edit_ts.get(777) == 5000.0  # backoff recorded
