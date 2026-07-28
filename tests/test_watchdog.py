"""Watchdog voice-recovery decision (_should_rejoin).

Guards against a dead/stuck Discord voice link — e.g. a 1006 server rotation
whose reconnect stalls — leaving the bot silent while SIP/recording keep
working. The watchdog must force a rejoin when we still intend to stream but the
link is down, and must NOT rejoin after a deliberate leave/panel-stop/admin kick
(all of which clear desired_channel_id).
"""
from __future__ import annotations


def test_default_state_has_no_rejoin_intent(bot_module):
    assert bot_module.GuildState().desired_channel_id is None


def test_rejoin_when_intending_to_stream_but_link_is_down(bot_module):
    m = bot_module
    gs = m.GuildState(desired_channel_id=123)
    assert m._should_rejoin(gs, connected=False) is True


def test_no_rejoin_while_connected(bot_module):
    m = bot_module
    gs = m.GuildState(desired_channel_id=123)
    assert m._should_rejoin(gs, connected=True) is False


def test_no_rejoin_after_deliberate_leave(bot_module):
    m = bot_module
    gs = m.GuildState(desired_channel_id=None)   # cleared by /leave, stop, kick
    assert m._should_rejoin(gs, connected=False) is False
