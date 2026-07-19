"""Target-repeater resolution for operator commands."""
from __future__ import annotations


def test_shared_channels_fall_back_to_active_preset(bot_module, cfg, make_ctx):
    bot_module.get_state(1).preset = "vhf"
    shared = cfg.repeater_by_id("vhf").discord.channel_id
    rpt, how = bot_module._resolve_target_repeater(make_ctx(shared), None)
    assert rpt.id == "vhf" and how == "active preset"


def test_explicit_argument_always_wins(bot_module, cfg, make_ctx):
    bot_module.get_state(1).preset = "vhf"
    rpt, how = bot_module._resolve_target_repeater(make_ctx(0), "uhf")
    assert rpt.id == "uhf" and how == "requested"


def test_unknown_explicit_lists_options(bot_module, cfg, make_ctx):
    rpt, how = bot_module._resolve_target_repeater(make_ctx(0), "hf")
    assert rpt is None and "vhf" in how and "uhf" in how


def test_split_channels_infer_uniquely(bot_module, cfg, make_ctx):
    bot_module.get_state(1).preset = "vhf"
    u = cfg.repeater_by_id("uhf")
    u.discord.channel_id = 999
    u.discord.activity_channel_id = 998
    for ch in (999, 998):
        rpt, how = bot_module._resolve_target_repeater(make_ctx(ch), None)
        assert rpt.id == "uhf" and how == "this channel"


def test_thread_parent_infers_repeater(bot_module, cfg, make_ctx):
    bot_module.get_state(1).preset = "vhf"
    cfg.repeater_by_id("uhf").discord.activity_channel_id = 998
    rpt, how = bot_module._resolve_target_repeater(make_ctx(555, parent_id=998), None)
    assert rpt.id == "uhf" and how == "this channel"


def test_unrelated_channel_falls_back(bot_module, cfg, make_ctx):
    bot_module.get_state(1).preset = "vhf"
    cfg.repeater_by_id("uhf").discord.channel_id = 999
    rpt, how = bot_module._resolve_target_repeater(make_ctx(12345), None)
    assert rpt.id == "vhf" and how == "active preset"


def test_target_note_names_repeater(bot_module, cfg):
    note = bot_module._target_note(cfg.repeater_by_id("uhf"), "this channel")
    assert note == "*(target: UHF · this channel)*"
