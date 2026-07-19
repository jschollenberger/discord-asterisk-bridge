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

qrz.py — QRZ.com XML API client.

Uses the QRZ XML data plan: authenticates with username + api_key to obtain a
session key, then performs callsign lookups against that session.  The session
key is cached in memory and re-acquired transparently on expiry.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Optional

import aiohttp

from config import BOT_VERSION

log = logging.getLogger("k2br.qrz")

QRZ_URL   = "https://xmldata.qrz.com/xml/current/"
# QRZ XML responses declare:  xmlns="http://xmldata.qrz.com"
# (NOT /xml/current/ — that suffix only appears in the API path, not the namespace)
_NS       = "http://xmldata.qrz.com"
_NS_MAP   = {"q": _NS}
_AGENT    = f"k2br-repeater-bot/{BOT_VERSION}"


class QRZError(Exception):
    """Raised when QRZ returns an error response."""


class QRZClient:
    """Async QRZ.com XML API client with automatic session management."""

    def __init__(self, username: str, api_key: str) -> None:
        self.username  = username
        self.api_key   = api_key
        self._session_key: Optional[str] = None

    # ── Session management ────────────────────────────────────────────────────

    async def _authenticate(self) -> None:
        params = {
            "username": self.username,
            "password": self.api_key,
            "agent":    _AGENT,
        }
        log.debug(
            f"QRZ auth request: GET {QRZ_URL}"
            f"?username={self.username}&password=***&agent={_AGENT}"
        )
        async with aiohttp.ClientSession() as s:
            async with s.get(QRZ_URL, params=params,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                status = r.status
                text   = await r.text()

        log.debug(f"QRZ auth HTTP status: {status}")
        log.debug(f"QRZ auth raw response:\n{text}")

        root  = _parse_xml(text)
        # QRZ uses <Error> in docs but real responses use <e> — check both
        error = _find(root, "Session/Error") or _find(root, "Session/e")
        if error:
            log.warning(f"QRZ auth returned an error element: {error!r}")
            raise QRZError(f"QRZ authentication failed: {error}")

        key = _find(root, "Session/Key")
        log.debug(f"QRZ session key found: {bool(key and key != '0')!r} (value={key!r})")

        if not key or key == "0":
            log.warning(
                f"QRZ auth did not return a usable session key. "
                f"Check username ({self.username!r}), API key, and that the account "
                f"has an active XML data subscription. Raw response logged above at DEBUG."
            )
            raise QRZError(
                "QRZ did not return a session key — check your username and API key "
                "in config.yaml, and ensure the account has a QRZ XML data subscription."
            )
        self._session_key = key
        log.info(f"QRZ authenticated successfully as {self.username!r}")

    # ── Public API ────────────────────────────────────────────────────────────

    async def lookup(self, callsign: str, _retried: bool = False) -> dict:
        """
        Look up a callsign and return a dict of fields.
        Re-authenticates automatically if the session has expired — but only
        once per call (_retried guards against a persistent, non-expiry
        "session"-flavored error looping forever instead of failing cleanly).
        """
        if not self._session_key:
            await self._authenticate()

        cs_upper = callsign.upper().strip()
        params   = {"s": self._session_key, "callsign": cs_upper}
        log.debug(f"QRZ lookup: {cs_upper}")

        async with aiohttp.ClientSession() as s:
            async with s.get(QRZ_URL, params=params,
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                status = r.status
                text   = await r.text()

        log.debug(f"QRZ lookup HTTP status: {status}")
        log.debug(f"QRZ lookup raw response:\n{text}")

        root = _parse_xml(text)

        # Handle session expiry — check both <Error> and <e> tag variants
        session_error = _find(root, "Session/Error") or _find(root, "Session/e")
        if session_error:
            log.warning(f"QRZ lookup session error: {session_error!r}")
            if not _retried and any(w in session_error.lower() for w in ("session", "timeout", "expired")):
                log.info("QRZ session expired — re-authenticating")
                self._session_key = None
                return await self.lookup(callsign, _retried=True)
            raise QRZError(f"QRZ error: {session_error}")

        cs = root.find("q:Callsign", _NS_MAP)
        if cs is None:
            log.debug(f"QRZ: no Callsign element found in response for {cs_upper!r}")
            raise QRZError(f"Callsign **{cs_upper}** not found in the QRZ database.")

        def g(tag: str) -> str:
            v = cs.findtext(f"q:{tag}", namespaces=_NS_MAP)
            return v.strip() if v else ""

        result = {
            "callsign":      g("call"),
            "fname":         g("fname"),
            "name":          g("name"),
            "addr1":         g("addr1"),
            "addr2":         g("addr2"),
            "state":         g("state"),
            "country":       g("country"),
            "lat":           g("lat"),
            "lon":           g("lon"),
            "grid":          g("grid"),
            "county":        g("county"),
            "license_class": g("class"),
            "expires":       g("expdate"),
            "email":         g("email"),
            "url":           g("url"),
            "image":         g("image"),
            "trustee":       g("trustee"),
            "aliases":       g("aliases"),
            "bio":           g("bio"),
        }
        log.info(f"QRZ lookup success: {result['callsign']} — {result['fname']} {result['name']} [{result['grid']}]")
        return result


# ─── XML helpers ──────────────────────────────────────────────────────────────

def _parse_xml(text: str) -> ET.Element:
    """Parse QRZ XML response, stripping namespace prefixes if needed."""
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise QRZError(f"Could not parse QRZ response: {e}") from e


def _find(root: ET.Element, path: str) -> Optional[str]:
    """Find text at a namespaced path, returning None if absent."""
    # Build a namespaced path like q:Session/q:Key → q:Session/q:Key
    ns_path = "/".join(f"q:{p}" for p in path.split("/"))
    el = root.find(ns_path, _NS_MAP)
    if el is None:
        # Fallback: try without namespace (some responses drop the NS)
        el = root.find(path)
    return el.text.strip() if el is not None and el.text else None
