"""Control-panel view invariants.

The buttons only dispatch (after a restart, or on a reposted panel) if the view
is *persistent* — timeout=None and every component has a custom_id. If a button
ever loses its custom_id, discord.py silently stops routing clicks to it, which
is invisible until someone presses it.
"""
from __future__ import annotations


def test_panel_view_is_persistent(bot_module):
    assert bot_module._panel_view.is_persistent()


def test_every_panel_button_has_a_custom_id(bot_module):
    ids = [getattr(item, "custom_id", None) for item in bot_module._panel_view.children]
    assert ids and all(cid for cid in ids)   # no None / empty custom_ids
    assert len(ids) == len(set(ids))         # and they're unique
