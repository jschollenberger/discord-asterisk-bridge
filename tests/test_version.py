"""
Running-build identity in the version string.

BOT_VERSION is the release semver; version_string() also surfaces `git describe`
when running from a source checkout ahead of the last tag, so the banner and the
/help About line show exactly which commit is live between releases.
config._format_version is the pure part, exercised here without invoking git.
"""
from __future__ import annotations

import config


def test_clean_release_has_no_build_suffix():
    # No build id, or the build id is exactly the release tag → just the version.
    assert config._format_version("1.3.0", None) == "1.3.0"
    assert config._format_version("1.3.0", "v1.3.0") == "1.3.0"
    assert config._format_version("1.3.0", "1.3.0") == "1.3.0"


def test_source_build_appends_git_describe():
    assert (
        config._format_version("1.3.0", "v1.3.0-9-gd385cf2")
        == "1.3.0 (build v1.3.0-9-gd385cf2)"
    )


def test_dirty_tree_is_surfaced():
    assert config._format_version("1.3.0", "v1.3.0-dirty") == "1.3.0 (build v1.3.0-dirty)"
    assert (
        config._format_version("1.3.0", "v1.3.0-9-gd385cf2-dirty")
        == "1.3.0 (build v1.3.0-9-gd385cf2-dirty)"
    )


def test_version_string_starts_with_bot_version():
    # Whatever git reports, the release semver is always the prefix.
    vs = config.version_string()
    assert isinstance(vs, str) and vs.startswith(config.BOT_VERSION)


def test_build_id_is_str_or_none():
    bid = config._build_id()
    assert bid is None or isinstance(bid, str)
