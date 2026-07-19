"""
K2BR Repeater Bot
Copyright (C) 2026 Jason Schollenberger / KD2QED
Licensed under GPLv3 — see LICENSE.

Shared pytest setup.

config.py loads config.yaml from the working directory at IMPORT time, so we
chdir into tests/fixtures (which holds a sanitized config with fake
credentials) before anything imports the application modules. The repo root
is added to sys.path so `import config` / `import allstar_discord_bot` work
without an install step.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
_FIXTURES  = _TESTS_DIR / "fixtures"

# Must happen before any test module imports the app modules.
os.chdir(_FIXTURES)
sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture()
def bot_module():
    """The bot module with its mutable global state reset around each test."""
    import allstar_discord_bot as m

    saved_monitors = dict(m._monitor_clients)
    saved_states   = dict(m._guild_states)
    saved_locks    = dict(m._tx_locks)
    m._monitor_clients.clear()
    m._guild_states.clear()
    m._tx_locks.clear()
    try:
        yield m
    finally:
        m._monitor_clients.clear();  m._monitor_clients.update(saved_monitors)
        m._guild_states.clear();     m._guild_states.update(saved_states)
        m._tx_locks.clear();         m._tx_locks.update(saved_locks)


@pytest.fixture()
def cfg():
    """The loaded fixture Config, with per-repeater discord bindings restored
    after each test (several tests mutate channel ids to simulate a split)."""
    import config as config_mod

    saved = [
        (r.id, r.discord.token, r.discord.channel_id, r.discord.activity_channel_id)
        for r in config_mod.cfg.repeaters
    ]
    try:
        yield config_mod.cfg
    finally:
        for rid, tok, ch, act in saved:
            r = config_mod.cfg.repeater_by_id(rid)
            r.discord.token = tok
            r.discord.channel_id = ch
            r.discord.activity_channel_id = act


class FakeChannel:
    def __init__(self, ch_id: int, parent_id: int | None = None):
        self.id = ch_id
        if parent_id is not None:
            self.parent_id = parent_id


class FakeGuild:
    def __init__(self, guild_id: int = 1):
        self.id = guild_id


class FakeCtx:
    """Minimal stand-in for commands.Context used by the resolver tests."""
    def __init__(self, ch_id: int, parent_id: int | None = None, guild_id: int = 1):
        self.channel = FakeChannel(ch_id, parent_id)
        self.guild = FakeGuild(guild_id)


@pytest.fixture()
def make_ctx():
    return FakeCtx
