"""
Voice auto-recovery after an unexpected drop.

on_voice_state_update must only surrender the rejoin intent (desired_channel_id)
on a *deliberate* stop. An unexpected drop — a 1006 rotation whose reconnect
stalled, a transient DNS/network blip, an admin kick — must KEEP it set so the
watchdog keeps retrying (_should_rejoin). Regression guard for 2026-08-18, when
a single `getaddrinfo failed` during a routine reconnect cleared the intent and
left the bot silently out of voice for ~10 hours.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


async def _noop(*_a, **_k):
    return None


def _fire_self_disconnect(m, monkeypatch, guild_id: int, desired: int | None):
    """Drive on_voice_state_update for the bot leaving voice (after.channel=None)
    with the given desired_channel_id, and return the resulting GuildState."""
    monkeypatch.setattr(m.asyncio, "sleep", _noop)   # skip the 1.5s settle wait

    guild = SimpleNamespace(id=guild_id, name="SCARA", voice_client=None)
    me = SimpleNamespace(guild=guild)
    # bot.user is a read-only property, so swap the module-level `bot` (the
    # handler resolves it as a global) for a stand-in whose .user is `me`, making
    # `member == bot.user` — i.e. this is the bot's own voice state, not a user's.
    monkeypatch.setattr(m, "bot", SimpleNamespace(user=me))

    gs = m.get_state(guild_id)
    gs.desired_channel_id = desired
    gs.streaming = True

    before = SimpleNamespace(channel=SimpleNamespace(name="Repeater Stream"))
    after = SimpleNamespace(channel=None)
    asyncio.run(m.on_voice_state_update(me, before, after))
    return gs


def test_unexpected_drop_keeps_rejoin_intent(bot_module, monkeypatch):
    # desired_channel_id set → the drop was not a deliberate stop.
    gs = _fire_self_disconnect(bot_module, monkeypatch, guild_id=98701, desired=555)
    assert gs.desired_channel_id == 555                       # intent preserved
    assert bot_module._should_rejoin(gs, connected=False) is True   # watchdog will rejoin
    assert gs.streaming is False                              # playback state still cleared


def test_deliberate_stop_stays_out(bot_module, monkeypatch):
    # /leave or panel-Stop cleared desired_channel_id before disconnecting.
    gs = _fire_self_disconnect(bot_module, monkeypatch, guild_id=98702, desired=None)
    assert gs.desired_channel_id is None                      # stays out
    assert bot_module._should_rejoin(gs, connected=False) is False


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
