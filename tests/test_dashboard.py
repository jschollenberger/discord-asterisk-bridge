"""Console dashboard renders every configured repeater in every state."""
from __future__ import annotations

import io

from rich.console import Console


def _render(bot_module) -> str:
    con = Console(file=io.StringIO(), width=130, force_terminal=False)
    con.print(bot_module.build_dashboard())
    return con.file.getvalue()


def test_all_repeaters_listed_before_monitors_start(bot_module, cfg):
    out = _render(bot_module)
    assert "vhf" in out and "uhf" in out
    assert out.count("Not started") == 2
    assert "146.745" in out and "448.775" in out


def test_monitor_states_and_roles_shown(bot_module, cfg):
    class FakeState:
        def __init__(self, n): self.name = n
    class FakeClient:
        def __init__(self, n): self.state = FakeState(n)
    bot_module._monitor_clients["vhf"] = FakeClient("CONNECTED")
    bot_module._monitor_clients["uhf"] = FakeClient("RECONNECTING")
    out = _render(bot_module)
    assert "Connected" in out and "Reconnecting" in out
    assert out.count("Monitoring") == 2   # nothing attached to voice in tests
