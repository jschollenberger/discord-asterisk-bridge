"""N-way cross-repeater link planning.

/link-repeaters and /unlink-repeaters no longer assume exactly two repeaters
named vhf/uhf. _link_plan decides, for any number of repeaters, which one is
the hub (issues the ilink commands via its AMI) and which are the targets
(linked to it by node). It's pure and parameterised so the 1/2/3+ and
misconfiguration cases can be tested without a live AMI.
"""
from __future__ import annotations

from types import SimpleNamespace


def _rpt(rid, node="1", ami=True, enabled=True):
    return SimpleNamespace(
        id=rid,
        allstar_node=node,
        ami=(SimpleNamespace(host="h", port=1, username="u", password="p") if ami else None),
        enabled=enabled,
        display_name=rid.upper(),
    )


def test_link_plan_with_fixture_config(bot_module, cfg):
    # Fixture: vhf + uhf, both with allstar_node + ami. vhf is first → hub;
    # uhf is the single target. Matches the old hardcoded behavior.
    hub, targets, err = bot_module._link_plan(cfg.repeaters)
    assert err is None
    assert hub.id == "vhf"
    assert [t.id for t in targets] == ["uhf"]


def test_link_plan_needs_at_least_two_linkable(bot_module):
    hub, targets, err = bot_module._link_plan([_rpt("a")])
    assert hub is None and targets == [] and err and "at least two" in err


def test_link_plan_needs_an_ami(bot_module):
    hub, targets, err = bot_module._link_plan([_rpt("a", ami=False), _rpt("b", ami=False)])
    assert hub is None and targets == [] and err and "ami" in err.lower()


def test_link_plan_hub_is_first_repeater_with_an_ami(bot_module):
    # a has no AMI, so b (first with one) is the hub; a and c are targets.
    hub, targets, err = bot_module._link_plan(
        [_rpt("a", ami=False), _rpt("b"), _rpt("c")]
    )
    assert err is None
    assert hub.id == "b"
    assert [t.id for t in targets] == ["a", "c"]


def test_link_plan_excludes_disabled_and_nodeless(bot_module):
    # Only enabled repeaters that actually have a node participate.
    hub, targets, err = bot_module._link_plan(
        [_rpt("a"), _rpt("b", node=""), _rpt("c", enabled=False), _rpt("d")]
    )
    assert err is None
    assert hub.id == "a"
    assert [t.id for t in targets] == ["d"]
