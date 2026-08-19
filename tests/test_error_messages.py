"""
on_command_error must never leak a raw exception to the user: known,
user-caused errors get a friendly one-liner; anything unexpected returns None so
the handler shows a generic "it's been logged" message and logs the traceback.
Regression guard for the /uhf case that dumped
`ClientException: Not connected to voice` at the user.
"""
from __future__ import annotations

import discord
from discord.ext import commands


def test_unexpected_wrapped_exception_is_not_leaked(bot_module):
    # Exactly the /uhf case: an internal exception wrapped by the invoke machinery.
    wrapped = commands.CommandInvokeError(discord.ClientException("Not connected to voice"))
    assert bot_module._friendly_command_error(wrapped) is None


def test_raw_runtime_error_is_not_leaked(bot_module):
    assert bot_module._friendly_command_error(RuntimeError("kaboom")) is None


def test_dm_only_is_friendly(bot_module):
    msg = bot_module._friendly_command_error(commands.NoPrivateMessage())
    assert msg and "server" in msg


def test_missing_permissions_is_friendly(bot_module):
    msg = bot_module._friendly_command_error(commands.MissingPermissions(["manage_guild"]))
    assert msg and "permission" in msg


def test_check_failure_is_friendly(bot_module):
    msg = bot_module._friendly_command_error(commands.CheckFailure())
    assert msg and msg.startswith("❌")


def test_bad_argument_message_is_shown(bot_module):
    # BadArgument text is written for humans, so it's safe to surface.
    msg = bot_module._friendly_command_error(commands.BadArgument("no such preset"))
    assert msg == "❌ no such preset"


def test_wrapper_is_unwrapped_to_the_friendly_cause(bot_module):
    # A recognizable error wrapped in an invoke error is still recognized.
    wrapped = commands.CommandInvokeError(commands.MissingPermissions(["x"]))
    msg = bot_module._friendly_command_error(wrapped)
    assert msg and "permission" in msg
