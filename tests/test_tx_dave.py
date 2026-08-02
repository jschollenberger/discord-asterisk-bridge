"""Opting out of Discord's DAVE end-to-end voice encryption.

If the optional `davey` library is installed, discord.py advertises DAVE and
Discord E2EE-encrypts inbound voice — which discord-ext-voice-recv can't
decode, so every packet fails Opus decode ("corrupted stream") and no TX audio
is ever relayed. _disable_dave_e2ee() forces discord.py to advertise
max_dave_protocol_version=0 so the channel downgrades to transport-only.
"""
from __future__ import annotations

import pytest

from discord import voice_state as vs


@pytest.fixture()
def restore_dave(bot_module):
    """Save/restore the third-party global and the one-shot guard we mutate."""
    saved_has_dave = getattr(vs, "has_dave", False)
    saved_guard = bot_module._dave_disabled
    try:
        yield
    finally:
        vs.has_dave = saved_has_dave
        bot_module._dave_disabled = saved_guard


def test_disable_dave_forces_max_version_to_zero(bot_module, restore_dave):
    vs.has_dave = True                       # simulate davey installed → DAVE on
    bot_module._dave_disabled = False
    bot_module._disable_dave_e2ee()
    assert vs.has_dave is False              # advertises max_dave_protocol_version=0
    assert bot_module._dave_disabled is True


def test_disable_dave_is_idempotent(bot_module, restore_dave):
    bot_module._dave_disabled = False
    bot_module._disable_dave_e2ee()
    # A second call is a no-op and must not throw even if davey re-appears.
    vs.has_dave = True
    bot_module._disable_dave_e2ee()
    assert vs.has_dave is True               # guard tripped → left untouched the 2nd time
