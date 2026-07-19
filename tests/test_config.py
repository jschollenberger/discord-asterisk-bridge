"""Config loading: per-repeater Discord binding inheritance and helpers."""
from __future__ import annotations


def test_defaults_inherit_from_bot(cfg):
    v = cfg.repeater_by_id("vhf")
    assert v.discord.token == cfg.bot.token
    assert v.discord.channel_id == cfg.bot.auto_join_channel_id
    assert not v.discord.is_dedicated(cfg.bot.token)


def test_no_satellites_by_default(cfg):
    assert cfg.satellite_repeaters() == []
    assert [r.id for r in cfg.primary_repeaters()] == ["vhf", "uhf"]


def test_dedicated_token_makes_satellite(cfg):
    u = cfg.repeater_by_id("uhf")
    u.discord.token = "SECOND_APP_TOKEN"
    assert [r.id for r in cfg.satellite_repeaters()] == ["uhf"]
    assert [r.id for r in cfg.primary_repeaters()] == ["vhf"]


def test_activity_channel_fallback_and_override(cfg):
    v = cfg.repeater_by_id("vhf")
    assert cfg.activity_channel_id_for(v) == cfg.activity.channel_id
    assert cfg.activity_channel_id_for(None) == cfg.activity.channel_id
    v.discord.activity_channel_id = 333000333
    assert cfg.activity_channel_id_for(v) == 333000333
    # the other repeater keeps the fallback
    assert cfg.activity_channel_id_for(cfg.repeater_by_id("uhf")) == cfg.activity.channel_id


def test_has_activity_channels_with_global_off(cfg):
    saved = cfg.activity.channel_id
    try:
        cfg.activity.channel_id = 0
        assert not cfg.has_activity_channels()
        cfg.repeater_by_id("vhf").discord.activity_channel_id = 42
        assert cfg.has_activity_channels()
    finally:
        cfg.activity.channel_id = saved


def test_repeater_command_scoping(cfg):
    wx = cfg.repeater_command_by_id("weather")
    assert wx.valid_for("uhf") and not wx.valid_for("vhf")
    t = cfg.repeater_command_by_id("time")
    assert t.valid_for("vhf") and t.valid_for("uhf")


def test_enabled_defaults_true_when_omitted(cfg):
    # The fixture omits `enabled:` entirely — absent must mean enabled, so
    # existing configs keep working after the streaming_now removal.
    assert all(r.enabled for r in cfg.repeaters)
    assert [r.id for r in cfg.enabled_repeaters()] == ["vhf", "uhf"]


def test_disabled_repeater_excluded_from_enabled(cfg):
    cfg.repeater_by_id("uhf").enabled = False
    try:
        assert [r.id for r in cfg.enabled_repeaters()] == ["vhf"]
    finally:
        cfg.repeater_by_id("uhf").enabled = True


def test_streaming_now_is_gone(cfg):
    # Guard against the stale flag creeping back: it could not track preset
    # switches and its job is now done by live runtime state.
    assert not hasattr(cfg.repeaters[0], "streaming_now")
