"""
K2BR Repeater Bot
Copyright (C) 2026 Jason Schollenberger / KD2QED

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

ami.py — Asterisk Manager Interface client and AllStar node activity monitor.

Each AMIClient opens a fresh TCP connection per command — simpler and more
reliable than multiplexing commands and events on a single persistent socket.
The NodeActivityMonitor uses a separate polling loop for the activity feed.

Validated HamVOIP commands (as of 2026-07):
    rpt nodes <node>     — comma-separated connected node list with T/R prefix
    rpt stats <node>     — full node statistics including connected nodes + uptime
    rpt linkslist <node> — tabular link list with chain length and peers
    rpt cmd <node> cop <n> [arg]   — COP commands: node's own operational state
    rpt cmd <node> ilink <n> [arg] — ILINK commands: connect/disconnect/monitor
                                       a *specific* remote node — this is what
                                       link/unlink/monitor features actually need,
                                       not cop (see AMIClient.cop()'s docstring —
                                       cop and ilink use overlapping numbers for
                                       unrelated things, e.g. cop 6 is PTT while
                                       ilink 6 is disconnect-all-links).
                                       Source: https://hamvoip.org/howto/allstar_functions.pdf
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from config import RepeaterConfig

log = logging.getLogger("k2br.ami")

_TIMEOUT = 10.0    # seconds for connect + read operations


# ─────────────────────────────────────────────────────────────────────────────
# AMI Client
# ─────────────────────────────────────────────────────────────────────────────

class AMIClient:
    """
    Minimal async Asterisk Manager Interface (AMI) client.

    Opens a fresh TCP connection per call to run_command(), authenticates,
    sends the action, reads the output, and disconnects.  This keeps state
    management trivial at the cost of a small per-command TCP overhead —
    acceptable for the infrequent operations this bot performs.
    """

    def __init__(self, host: str, port: int, username: str, password: str):
        self.host     = host
        self.port     = port
        self.username = username
        self.password = password

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """
        Open TCP connection and authenticate with the AMI.

        Any failure after the socket is open (banner read timeout, a drain()
        failure, auth response timeout, or an explicit auth rejection) closes
        the connection before raising — without this, a slow or flaky AMI
        port would leak a socket on every failed attempt, and this runs
        every 15s via the activity feed poll by default.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=_TIMEOUT,
        )
        try:
            # Read the AMI banner: "Asterisk Call Manager/X.X.X\r\n"
            await asyncio.wait_for(reader.readline(), timeout=_TIMEOUT)

            # Authenticate
            self._write(writer, {
                "Action":   "Login",
                "Username": self.username,
                "Secret":   self.password,
            })
            await writer.drain()

            resp = await self._read_block(reader)
            if resp.get("Response") != "Success":
                raise ConnectionError(
                    f"AMI auth failed on {self.host}:{self.port} — "
                    f"{resp.get('Message', 'no message')}"
                )
        except Exception:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            raise

        return reader, writer

    def _write(self, writer: asyncio.StreamWriter, fields: dict) -> None:
        msg = "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
        writer.write(msg.encode())

    async def _read_block(self, reader: asyncio.StreamReader) -> dict[str, str]:
        """Read one AMI message block (lines until a blank line)."""
        fields: dict[str, str] = {}
        while True:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=_TIMEOUT)
            except asyncio.TimeoutError:
                return fields
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                return fields
            if ":" in line:
                k, _, v = line.partition(":")
                fields[k.strip()] = v.strip()

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        try:
            self._write(writer, {"Action": "Logoff"})
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _read_command_output(
        self, reader: asyncio.StreamReader, verbose: bool = True, command: str = ""
    ) -> str:
        """
        Read AMI Action: Command response lines until --END COMMAND--.

        HamVOIP sends output lines prefixed with "Output: " (standard AMI format).
        We also capture unprefixed lines as a fallback for non-standard builds.
        When verbose=True the raw exchange is logged at DEBUG for diagnostics.
        """
        raw_lines:    list[str] = []
        output_lines: list[str] = []
        reading = False

        while True:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=_TIMEOUT)
            except asyncio.TimeoutError:
                if verbose:
                    log.debug(f"AMI read timeout waiting for response to: {command!r}")
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if verbose:
                raw_lines.append(line)

            if "--END COMMAND--" in line:
                break
            if line.startswith("Response:"):
                reading = True
                continue
            if not reading:
                continue
            if line.startswith("Output:"):
                output_lines.append(line[7:].strip() if len(line) > 7 else "")
            elif not line.startswith(("Privilege:", "ActionID:", "Message:")):
                if line:
                    output_lines.append(line)

        if verbose:
            log.debug(f"AMI raw exchange for {command!r}: {raw_lines!r}")
        return "\n".join(output_lines)

    # ── Public API ───────────────────────────────────────────────────────────

    async def run_command(self, command: str) -> str:
        """Execute an Asterisk CLI command and return its text output (with debug logging)."""
        reader, writer = await self._open()
        try:
            self._write(writer, {"Action": "Command", "Command": command})
            await writer.drain()
            return await self._read_command_output(reader, verbose=True, command=command)
        finally:
            await self._close(writer)

    async def _run_quiet(self, command: str) -> str:
        """
        Execute a command without debug logging.
        Used for high-frequency polling to avoid flooding the log file.
        """
        reader, writer = await self._open()
        try:
            self._write(writer, {"Action": "Command", "Command": command})
            await writer.drain()
            return await self._read_command_output(reader, verbose=False)
        finally:
            await self._close(writer)

    async def cop(self, node: str, command: str) -> None:
        """
        Execute an AllStar COP (Control Operator Panel) command and log it.

        COP commands control the node's own operational state — NOT specific
        remote-node links (use ilink() below for connect/disconnect/monitor
        of a specific node). Selected COP commands, per HamVOIP's own
        function reference (https://hamvoip.org/howto/allstar_functions.pdf):
            cop 1   — System warm boot
            cop 2   — System enable
            cop 3   — System disable
            cop 6   — PTT (phone mode only), # to release
            cop 7   — Time out timer enable
            cop 8   — Time out timer disable
            cop 21  — Enable Parrot Mode
            cop 55  — Parrot once (if parrot mode is disabled)
        Full list (65 commands) is in the linked PDF — this bot currently
        only uses cop,55 (via config.yaml's repeater_commands "parrot"
        entry, if uncommented). Do not use cop 1/2/3/6/7/8 for link
        management — see ilink() instead; those numbers do something
        different under cop than they do under ilink, and some (1, 6) are
        genuinely disruptive if triggered by mistake (system reboot, PTT key).
        """
        full_cmd = f"rpt cmd {node} cop {command}"
        log.info(f"AMI → {full_cmd}  [{self.host}]")
        output = await self.run_command(full_cmd)
        if output:
            log.debug(f"AMI COP response: {output!r}")

    async def ilink(self, node: str, command: str) -> None:
        """
        Execute an AllStar ILINK command and log it — this is the correct
        family for connecting/disconnecting/monitoring a specific remote
        node, per HamVOIP's function reference (see cop() docstring for the
        source). Commonly used here:
            ilink 1  <node>  — Disconnect specified link
            ilink 2  <node>  — Connect specified link, monitor only
            ilink 3  <node>  — Connect specified link, transceive
            ilink 6          — Disconnect all links
            ilink 11 <node>  — Disconnect a previously permanently connected link
            ilink 12 <node>  — Permanently connect specified link, monitor only
            ilink 13 <node>  — Permanently connect specified link, transceive
        """
        full_cmd = f"rpt cmd {node} ilink {command}"
        log.info(f"AMI → {full_cmd}  [{self.host}]")
        output = await self.run_command(full_cmd)
        if output:
            log.debug(f"AMI ILINK response: {output!r}")

    async def get_connected_nodes(self, node: str) -> set[str]:
        """
        Return the set of AllStar node IDs currently linked to `node`.

        Uses 'rpt nodes <node>' which returns a comma-separated list like:
            T1999, T50719, T53209
        where T = transceive, R = receive-only (monitor), L = local.
        Own node and non-numeric entries are excluded.
        """
        output = await self.run_command(f"rpt nodes {node}")
        log.debug(f"rpt nodes {node} → {output!r}")
        return _parse_nodes(output, own_node=node)

    async def get_node_list_raw(self, node: str) -> str:
        """
        Return the raw connected-node line from 'rpt nodes' for display.
        Preserves the T/R mode prefix so callers can show connection type.
        Returns 'No connections' if the node list is empty.
        """
        output = await self.run_command(f"rpt nodes {node}")
        for line in output.splitlines():
            line = line.strip()
            # Skip headers/dividers, find the actual node list line
            if line and not line.startswith('*') and not line.startswith('-'):
                return line
        return "No connections"

    async def get_status_text(self, node: str) -> str:
        """
        Return 'rpt stats <node>' output for display in Discord.
        Provides uptime, scheduler state, connected nodes, autopatch status, etc.
        """
        return await self.run_command(f"rpt stats {node}")


# ─────────────────────────────────────────────────────────────────────────────
# Node list parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_nodes(output: str, own_node: str) -> set[str]:
    """
    Parse 'rpt nodes <node>' output and return a set of connected node IDs.

    HamVOIP 'rpt nodes' returns:
        ************************* CONNECTED NODES *************************
        T1999, T50719, T53209

    Node IDs are prefixed with their connection mode:
        T = Transceive (full duplex — bidirectional audio + PTT)
        R = Receive-only (monitor mode — audio only, no PTT)
        L = Local (on the same Asterisk instance)

    Own node and non-numeric entries are excluded from the result.
    """
    nodes: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        # Skip blank lines, header banners, and column dividers
        if not line or line.startswith('*') or line.startswith('-'):
            continue
        # Parse comma-separated list of mode-prefixed node IDs
        for part in line.split(','):
            part = part.strip()
            if not part:
                continue
            # Strip connection mode prefix (T / R / L)
            if part[0].upper() in ('T', 'R', 'L'):
                node_id = part[1:].strip()
            else:
                node_id = part
            # Keep only valid 4-6 digit node numbers, excluding own node
            if node_id.isdigit() and len(node_id) in range(4, 7) and node_id != own_node:
                nodes.add(node_id)
    return nodes


# ─────────────────────────────────────────────────────────────────────────────
# Node Activity Monitor
# ─────────────────────────────────────────────────────────────────────────────

class NodeActivityMonitor:
    """
    Polls each configured AllStar repeater node for connection changes and
    posts link/unlink events to a Discord channel.

    On the first successful poll per repeater, the monitor records the current
    state silently (no posts) so existing links don't trigger false events at
    startup.
    """

    def __init__(self) -> None:
        self._state:       dict[str, set[str]] = {}   # repeater_id → connected nodes
        self._last_polled: dict[str, float]    = {}   # repeater_id → unix timestamp
        self._initialized: set[str]            = set()

    async def poll(
        self,
        repeaters: list[RepeaterConfig],
        bot,
        channel_id_for,
    ) -> None:
        """
        Poll all repeaters and post any node link/unlink events.

        channel_id_for: callable (RepeaterConfig → int) resolving each
        repeater's activity channel id — per-repeater channel if configured,
        else the global one, 0 for none (post nothing for that repeater).
        Passing a resolver instead of a single id keeps this module free of
        any dependency on the config module's structure.
        """
        for rpt in repeaters:
            if not rpt.allstar_node or not rpt.ami:
                continue

            try:
                client = AMIClient(
                    rpt.ami.host, rpt.ami.port,
                    rpt.ami.username, rpt.ami.password,
                )
                # _run_quiet suppresses per-poll debug noise; use 'rpt nodes'
                # which is the validated HamVOIP command for listing linked nodes.
                raw     = await client._run_quiet(f"rpt nodes {rpt.allstar_node}")
                current = _parse_nodes(raw, own_node=rpt.allstar_node)
                self._last_polled[rpt.id] = time.time()
            except Exception as exc:
                log.debug(f"Activity poll error [{rpt.id}]: {exc}")
                continue

            if rpt.id not in self._initialized:
                # First successful poll: record baseline silently
                self._state[rpt.id] = current
                self._initialized.add(rpt.id)
                nodes_str = ", ".join(sorted(current)) if current else "none"
                log.info(
                    f"Node monitor ready [{rpt.display_name} / {rpt.allstar_node}]: "
                    f"{len(current)} node(s) linked: {nodes_str}"
                )
                continue

            prev     = self._state[rpt.id]
            linked   = current - prev
            unlinked = prev - current
            self._state[rpt.id] = current

            ch_id   = channel_id_for(rpt)
            channel = bot.get_channel(ch_id) if ch_id else None
            if not channel:
                continue

            if linked:
                await _post(channel, _fmt_node_event("🔗", "linked to", linked, rpt))
                log.info(
                    f"Activity: {len(linked)} node(s) linked to {rpt.id} ({rpt.allstar_node}): "
                    f"{', '.join(sorted(linked))}"
                )

            if unlinked:
                await _post(channel, _fmt_node_event("🔌", "unlinked from", unlinked, rpt))
                log.info(
                    f"Activity: {len(unlinked)} node(s) unlinked from {rpt.id} ({rpt.allstar_node}): "
                    f"{', '.join(sorted(unlinked))}"
                )

    def snapshot(self) -> dict[str, set[str]]:
        """Return current known node state for all repeaters."""
        return {k: set(v) for k, v in self._state.items()}

    def poll_age(self, repeater_id: str) -> Optional[float]:
        """Seconds since last successful poll, or None if never polled."""
        ts = self._last_polled.get(repeater_id)
        return (time.time() - ts) if ts is not None else None


def _fmt_node_event(emoji: str, verb: str, nodes: set[str], rpt) -> str:
    """
    Format a batched node link/unlink message.

    Single node:   🔗 **Node `53209` linked to UHF** (448.775 MHz · Node 50420)
    Multiple nodes: 🔗 **35 nodes linked to UHF** (448.775 MHz · Node 50420)
                   `1003`, `1023`, `1052`, …
    """
    count  = len(nodes)
    rpt_str = f"**{rpt.display_name}** ({rpt.frequency_mhz:.3f} MHz · Node {rpt.allstar_node})"

    if count == 1:
        node = next(iter(nodes))
        return f"{emoji} **Node `{node}` {verb}** {rpt_str}"

    # Multiple nodes — list them on a second line
    header  = f"{emoji} **{count} nodes {verb}** {rpt_str}"
    parts   = [f"`{n}`" for n in sorted(nodes, key=lambda x: x.zfill(10))]
    joined  = ", ".join(parts)

    # Respect Discord's 2000-char message limit
    max_body = 1900 - len(header)
    if len(joined) > max_body:
        truncated: list[str] = []
        used = 0
        for p in parts:
            needed = len(p) + (2 if truncated else 0)
            if used + needed > max_body - 15:
                truncated.append(f"… +{count - len(truncated)} more")
                break
            truncated.append(p)
            used += needed
        joined = ", ".join(truncated)

    return f"{header}\n{joined}"


async def _post(channel, text: str) -> None:
    try:
        await channel.send(text)
    except Exception as exc:
        log.warning(f"Activity channel post failed: {exc}")


# Module-level singleton used by the bot
monitor = NodeActivityMonitor()
