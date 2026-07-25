"""Autocomplete guards against Discord's limits: at most 25 choices per
response, and a choice display name of at most 100 chars.

Discord rejects an over-limit autocomplete response outright — the user then
sees *no* suggestions — so an unbounded list (cfg.repeater_commands can exceed
25; HamVOIP exposes 65+ function codes) would silently break the picker.
"""
from __future__ import annotations

import asyncio
import types

from config import RepeaterCommand


def test_choice_clamps_long_display_name(bot_module):
    ch = bot_module._choice("x" * 250, "val")
    assert len(ch.name) <= bot_module.AUTOCOMPLETE_CHOICE_NAME_MAX
    assert ch.value == "val"


def test_choice_leaves_short_name_untouched(bot_module):
    ch = bot_module._choice("Announce Time (time)", "time")
    assert ch.name == "Announce Time (time)"


def test_repeater_cmd_autocomplete_caps_at_25(bot_module, monkeypatch):
    m = bot_module
    many = [
        RepeaterCommand(id=f"cmd{i}", label=f"Command {i}", description="",
                        command="rpt fun {node} *00", repeaters=[])   # valid for all
        for i in range(40)
    ]
    monkeypatch.setattr(m.cfg, "repeater_commands", many)
    ix = types.SimpleNamespace(guild=types.SimpleNamespace(id=1))
    choices = asyncio.run(m._repeater_cmd_autocomplete(ix, ""))   # "" matches all
    assert len(choices) == m.AUTOCOMPLETE_MAX_CHOICES == 25
