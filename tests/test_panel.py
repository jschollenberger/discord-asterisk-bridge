"""Control-panel view invariants.

Two things must hold or the buttons silently stop working (no callback, no
error, just "didn't respond in time"):

1. The view must be *persistent* — timeout=None and every button has a
   custom_id — or discord.py won't route clicks to it after a restart.
2. The view must be instantiated INSIDE the event loop, not at module import.
   A discord.ui.View built with no running loop gets an internal __stopped=None,
   and discord.py's _dispatch_item then bails on every interaction. So the
   module must NOT create the panel at import time (it's created in on_ready).
"""
from __future__ import annotations


def test_panel_view_not_instantiated_at_import(bot_module):
    # If this ever becomes non-None, someone moved creation back to import time
    # (no running loop) and the buttons will silently stop dispatching.
    assert bot_module._panel_view is None


def test_panel_view_is_persistent(bot_module):
    assert bot_module.ControlPanelView().is_persistent()


def test_every_panel_button_has_a_unique_custom_id(bot_module):
    ids = [getattr(item, "custom_id", None) for item in bot_module.ControlPanelView().children]
    assert ids and all(cid for cid in ids)   # no None / empty custom_ids
    assert len(ids) == len(set(ids))         # and they're unique


def test_panel_has_no_standalone_start_button(bot_module):
    # The VHF/UHF preset buttons ARE the start action (clicking one joins the
    # repeater's configured channel and streams it), so there is no separate
    # Start button. If cp_start comes back, the self-start presets probably got
    # reverted — see _switch_via_panel's idle branch.
    ids = {getattr(item, "custom_id", None) for item in bot_module.ControlPanelView().children}
    assert "cp_start" not in ids
    assert {"cp_vhf", "cp_uhf"} <= ids   # the presets that replaced it
