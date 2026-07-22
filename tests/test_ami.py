"""AMI 'Command' response parsing: error detection vs. successful output.

Guards the behavior that a rejected AMI command (Response: Error) raises
AMICommandError instead of returning empty output that looks like success —
the gap that made a silently-rejected repeater command indistinguishable from
one that ran.
"""
from __future__ import annotations

import asyncio

import pytest

from ami import AMIClient, AMICommandError


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
