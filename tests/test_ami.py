"""AMI 'Command' response parsing: error detection vs. successful output.

Guards the behavior that a rejected AMI command (Response: Error) raises
AMICommandError instead of returning empty output that looks like success —
the gap that made a silently-rejected repeater command indistinguishable from
one that ran.
"""
from __future__ import annotations

import asyncio

import pytest

from ami import AMIClient, AMICommandError, _parse_nodes


def _read(data: bytes) -> str:
    """Drive _read_command_output over a canned AMI reply and return its output."""
    async def go() -> str:
        reader = asyncio.StreamReader()   # needs a running loop (created here)
        reader.feed_data(data)
        reader.feed_eof()
        client = AMIClient("host", 5038, "user", "pass")
        return await client._read_command_output(reader, verbose=False)

    return asyncio.run(go())


def test_error_response_raises_with_message():
    with pytest.raises(AMICommandError, match="Permission denied"):
        _read(b"Response: Error\r\nMessage: Permission denied\r\n\r\n")


def test_error_without_message_still_raises():
    with pytest.raises(AMICommandError):
        _read(b"Response: Error\r\n\r\n")


def test_follows_returns_output_lines():
    out = _read(
        b"Response: Follows\r\nPrivilege: Command\r\n"
        b"Output: line one\r\nOutput: line two\r\n--END COMMAND--\r\n"
    )
    assert out == "line one\nline two"


def test_follows_no_output_is_empty_not_error():
    # A successful 'rpt fun' DTMF injection: Follows, no Output lines, terminator.
    # Must return "" and must NOT raise (this is what the live VHF command did).
    out = _read(b"Response: Follows\r\nPrivilege: Command\r\n--END COMMAND--\r\n")
    assert out == ""


# Includes a 7-digit private node and a named peer, which must NOT be dropped.
_NODES = "********* CONNECTED NODES *********\nT1999, T50719, TDISCORD, R53209, T2000123, TPHONE"


def test_parse_nodes_keeps_all_but_own_discord_and_hidden():
    # Only own node and the bot's own DISCORD entry are excluded by default;
    # everything else is kept regardless of format (7-digit private, named peer).
    assert _parse_nodes(_NODES, own_node="50420") == {"1999", "50719", "53209", "2000123", "PHONE"}
    # hidden nodes (e.g. internal EchoLink 1999) additionally excluded.
    assert _parse_nodes(_NODES, own_node="50420", hidden=frozenset({"1999"})) == {
        "50719", "53209", "2000123", "PHONE"}
    # own node is dropped even when present in the list.
    assert _parse_nodes("T50420, T50719", own_node="50420") == {"50719"}
