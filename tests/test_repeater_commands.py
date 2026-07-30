"""Per-repeater /<id> shortcut command generation.

The bot no longer hardcodes /vhf and /uhf; it generates one /<id> hybrid
command per playable repeater from config. _build_repeater_command_specs is the
pure decision function — it picks which shortcuts to register given the set of
already-used command names — so it can be tested without touching the live
command tree.
"""
from __future__ import annotations


def test_one_shortcut_per_playable_repeater(bot_module):
    # Fixture config has vhf + uhf, both playable → both get a /<id> shortcut
    # named after their config id, with the frequency in the description.
    specs = bot_module._build_repeater_command_specs(reserved=set())
    by_id = {rpt_id: (name, desc) for name, desc, rpt_id in specs}

    assert set(by_id) == {r.id for r in bot_module._panel_presets()}
    assert by_id["vhf"][0] == "vhf"          # slash name == config id
    assert by_id["uhf"][0] == "uhf"
    assert "146.745" in by_id["vhf"][1]      # frequency carried into description
    assert "MHz" in by_id["uhf"][1]


def test_name_collision_is_skipped(bot_module):
    # If a repeater id collides with an existing command name, no shortcut is
    # generated for it (it stays reachable via /stream) — but others still are.
    specs = bot_module._build_repeater_command_specs(reserved={"vhf"})
    names = {name for name, _d, _r in specs}
    ids   = {rpt_id for _n, _d, rpt_id in specs}
    assert "vhf" not in names
    assert "uhf" in ids            # the non-colliding one is unaffected


def test_no_duplicate_names_across_generated_specs(bot_module):
    specs = bot_module._build_repeater_command_specs(reserved=set())
    names = [name for name, _d, _r in specs]
    assert len(names) == len(set(names))


def test_specs_respect_the_reserved_builtin_names(bot_module):
    # The live registration passes the real built-in command names as reserved;
    # none of the generated shortcuts may shadow a built-in.
    builtins = {"stream", "join", "leave", "help", "presets", "reconnect", "panel"}
    specs = bot_module._build_repeater_command_specs(reserved=builtins)
    names = {name for name, _d, _r in specs}
    assert not (names & builtins)
