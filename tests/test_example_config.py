"""
config_example.yaml must stay loadable and in sync with config.py.

A shipped example config that no longer parses (or has drifted from the
dataclasses) is a first-run failure for anyone else deploying this — and
it's silent until someone tries. This walks the real loader over it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config as config_mod

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = _REPO_ROOT / "config_example.yaml"


@pytest.mark.skipif(not _EXAMPLE.exists(), reason="config_example.yaml not present")
def test_example_config_loads():
    cfg = config_mod.load(_EXAMPLE)
    assert cfg.repeaters, "example should define at least one repeater"
    assert cfg.default_preset == cfg.repeaters[0].id
    # Every repeater must get a fully-resolved discord binding.
    for r in cfg.repeaters:
        assert r.discord is not None
        assert r.discord.token == cfg.bot.token
    # The knob documented in the example must be a level the logger accepts.
    assert cfg.bot.log_file_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@pytest.mark.skipif(not _EXAMPLE.exists(), reason="config_example.yaml not present")
def test_example_config_ships_no_real_secrets():
    """Guard against pasting a live config over the example."""
    text = _EXAMPLE.read_text(encoding="utf-8")
    # Discord bot tokens are long dotted base64; placeholders are short.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("token:"):
            value = stripped.split(":", 1)[1].strip().strip('"\'')
            assert len(value) < 40, f"example config appears to contain a real token: {line!r}"
