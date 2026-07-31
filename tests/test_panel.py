"""Control-panel view invariants.

Two things must hold or the buttons silently stop working (no callback, no
error, just "didn't respond in time"):

1. The view must be *persistent* — timeout=None and every button has a
   custom_id — or discord.py won't route clicks to it after a restart.
2. The view must be instantiated INSIDE the event loop, not at module import.
   A discord.ui.View built with no running loop gets an internal __stopped=None,
   and discord.py's _dispatch_item then bails on every interaction. So the
   module must NOT create the panel at import time (it's created in on_ready).

The preset buttons are built dynamically from cfg.repeaters, so the panel
adapts to however many repeaters a club runs rather than assuming a fixed
VHF/UHF pair.
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


def test_panel_has_one_green_button_per_playable_repeater(bot_module):
    # The presets are generated from config, not hardcoded to vhf/uhf. There
    # should be exactly one button per playable repeater, each keyed
    # cp_preset_<id>, plus the fixed Reconnect + Stop controls.
    presets = bot_module._panel_presets()
    assert presets, "fixture config should have at least one playable repeater"

    view = bot_module.ControlPanelView()
    preset_ids = {
        item.custom_id for item in view.children
        if getattr(item, "custom_id", "").startswith("cp_preset_")
    }
    assert preset_ids == {f"cp_preset_{r.id}" for r in presets}

    # No leftover hardcoded ids from the old two-button / Start-button panel.
    all_ids = {getattr(item, "custom_id", None) for item in view.children}
    assert not ({"cp_vhf", "cp_uhf", "cp_start"} & all_ids)
    assert {"cp_reconnect", "cp_stop"} <= all_ids


def test_panel_controls_sit_below_the_presets(bot_module):
    # Reconnect and Stop are pinned to the bottom row so they always follow the
    # presets regardless of how many there are.
    view = bot_module.ControlPanelView()
    rows = {
        item.custom_id: item.row
        for item in view.children
        if item.custom_id in ("cp_reconnect", "cp_stop")
    }
    assert rows.get("cp_reconnect") == 4
    assert rows.get("cp_stop") == 4


def test_panel_preset_count_is_capped(bot_module):
    # However many repeaters a club configures, the panel never emits more
    # preset buttons than the action-row grid can hold alongside the controls.
    view = bot_module.ControlPanelView()
    preset_buttons = [
        item for item in view.children
        if getattr(item, "custom_id", "").startswith("cp_preset_")
    ]
    assert len(preset_buttons) <= bot_module._PANEL_MAX_PRESETS
