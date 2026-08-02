"""Startup channel summary — lists every configured Discord channel and its
definer (global default vs a repeater's own `discord:` override), so a club
that only sets per-repeater channels can see they're actually configured
instead of just a blank global line.
"""
from __future__ import annotations


def test_summary_lists_defaults_and_every_repeater(bot_module):
    lines = bot_module._channel_summary_lines()
    text = "\n".join(lines)
    assert "Stream voice — default" in text
    assert "Activity — default" in text
    # One voice line and one activity line per configured repeater.
    for r in bot_module.cfg.repeaters:
        assert sum(1 for line in lines if line.strip().startswith(f"{r.id}:")) == 2


def test_fixture_config_is_all_default(bot_module):
    # The fixture sets only global channels (no per-repeater overrides), so
    # every repeater line should be tagged (default).
    lines = bot_module._channel_summary_lines()
    repeater_lines = [line for line in lines if line.strip().startswith(("vhf:", "uhf:"))]
    assert repeater_lines and all("(default)" in line for line in repeater_lines)


def test_per_repeater_overrides_are_flagged(bot_module, cfg):
    # cfg fixture restores discord bindings afterwards.
    vhf = cfg.repeater_by_id("vhf")
    vhf.discord.channel_id = 999           # differs from the global default
    vhf.discord.activity_channel_id = 888  # its own activity channel

    lines = bot_module._channel_summary_lines()
    voice_vhf = next(line for line in lines if line.strip().startswith("vhf:") and "999" in line)
    act_vhf   = next(line for line in lines if "888" in line)
    assert "(per-repeater)" in voice_vhf
    assert "(per-repeater)" in act_vhf

    # uhf still inherits → stays (default)
    uhf_lines = [line for line in lines if line.strip().startswith("uhf:")]
    assert all("(default)" in line for line in uhf_lines)
