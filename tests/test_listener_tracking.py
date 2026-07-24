"""Voice-channel listener tracking: entries must not leak when the bot leaves.

Joins are recorded in _voice_listeners and normally popped when the listener
leaves the bot's channel — but that pop only fires while the bot is still there.
When the BOT disconnects (/leave, panel stop, a true voice drop), every
disconnect path routes through _clear_audio_client(), which must drop that
guild's tracked listeners or their entries linger for the process lifetime.
"""
from __future__ import annotations

import time


def test_clear_audio_client_drops_only_this_guilds_listeners(bot_module):
    m = bot_module
    saved = dict(m._voice_listeners)
    m._voice_listeners.clear()
    try:
        m._voice_listeners[(1, 100)] = time.time()
        m._voice_listeners[(1, 101)] = time.time()
        m._voice_listeners[(2, 200)] = time.time()   # a different guild

        m._clear_audio_client(1)

        assert (1, 100) not in m._voice_listeners
        assert (1, 101) not in m._voice_listeners
        assert (2, 200) in m._voice_listeners         # untouched
    finally:
        m._voice_listeners.clear()
        m._voice_listeners.update(saved)


def test_clear_audio_client_no_listeners_is_safe(bot_module):
    """No tracked listeners for the guild → clean no-op (still releases TX etc.)."""
    m = bot_module
    saved = dict(m._voice_listeners)
    m._voice_listeners.clear()
    try:
        m._clear_audio_client(99)          # nothing tracked for this guild
        assert m._voice_listeners == {}
    finally:
        m._voice_listeners.clear()
        m._voice_listeners.update(saved)
