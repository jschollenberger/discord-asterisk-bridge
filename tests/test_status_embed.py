"""_status_embed length safety: a repeater linked to a busy net must not blow
past Discord's 1024-char embed-field limit.

If the "Repeaters" field exceeds 1024 chars the whole embed send raises, so
/repeater-status would show nothing at all — the same "emitted but never seen"
class as the node-event truncation. _join_capped bounds the linked-node list
per repeater, and a hard backstop guarantees the field is always valid.
"""
from __future__ import annotations

import re


def test_join_capped_short_list_is_unchanged(bot_module):
    assert bot_module._join_capped(["a", "b", "c"], 100) == "a, b, c"


def test_join_capped_empty_is_empty(bot_module):
    assert bot_module._join_capped([], 100) == ""


def test_join_capped_truncates_within_budget_with_count(bot_module):
    items = [f"`{n}`" for n in range(100)]
    out = bot_module._join_capped(items, 60)
    assert len(out) <= 60
    m = re.search(r"\+(\d+) more", out)
    assert m is not None and int(m.group(1)) > 0


def test_status_embed_repeaters_field_stays_within_discord_limit(bot_module):
    m = bot_module
    rid = m.cfg.repeaters[0].id
    saved = dict(m.node_monitor._state)
    m.node_monitor._state[rid] = {str(n) for n in range(100000, 100600)}   # 600 nodes
    try:
        e = m._status_embed(1)
        field = next(f for f in e.fields if f.name == "Repeaters")
        assert len(field.value) <= m.EMBED_FIELD_MAX
        assert "more" in field.value          # per-repeater truncation notice shown
    finally:
        m.node_monitor._state.clear()
        m.node_monitor._state.update(saved)
