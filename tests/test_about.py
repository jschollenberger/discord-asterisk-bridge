"""Accreditation surfaces: the /help About line and the source/issue buttons.

The bot must carry a visible link back to the project source (GPLv3), so guard
that the About line names the project + version and the help view links to the
repo.
"""
from __future__ import annotations


def test_about_line_names_project_version_author_license(bot_module):
    m = bot_module
    line = m._about_line()
    assert m.PROJECT_NAME in line
    assert m.version_string() in line   # release semver, plus git build id off-release
    assert m.AUTHOR in line
    assert m.LICENSE in line


def test_help_view_links_to_source_and_issues(bot_module):
    m = bot_module
    view = m._help_links_view()
    urls = [getattr(c, "url", None) for c in view.children]
    assert m.SOURCE_URL in urls
    assert any(u and u.endswith("/issues") for u in urls)


def test_display_name_uses_club_name_not_callsign(bot_module):
    # BOT_NAME is the per-deployment display name; it should track club.name so
    # other clubs get their own, not the hardcoded callsign.
    m = bot_module
    assert m.BOT_NAME == f"{m.cfg.club.name} Repeater Bot"
