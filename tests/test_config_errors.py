"""Config loading surfaces user-fixable problems as ConfigError.

Running the bot without a config.yaml (or with a malformed one) should read as
"here's what to fix", not a Python traceback. load() raises a typed ConfigError
with a human-readable message; the import-time singleton turns that into a
clean message + non-zero exit (covered by the module-level handler, not here).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import config as config_mod


def test_missing_config_raises_config_error(tmp_path: Path):
    missing = tmp_path / "config.yaml"
    with pytest.raises(config_mod.ConfigError) as ei:
        config_mod.load(missing)
    msg = str(ei.value)
    assert "config.yaml" in msg
    assert "config.example.yaml" in msg   # tells the user what to do


def test_invalid_yaml_raises_config_error(tmp_path: Path):
    bad = tmp_path / "config.yaml"
    bad.write_text("bot: [unterminated\n")   # not valid YAML
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(bad)


def test_non_mapping_config_raises_config_error(tmp_path: Path):
    notamap = tmp_path / "config.yaml"
    notamap.write_text("just a string\n")   # valid YAML, but not a mapping
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(notamap)
