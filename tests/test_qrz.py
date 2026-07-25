"""QRZ XML client: the parse helpers, the <e> vs <Error> element fallback, and
the session-expiry re-auth path.

QRZ's real responses use <e> where the docs say <Error>, and a session key
silently expires and must be re-acquired mid-flight — both easy to regress and
both invisible until a live lookup fails, so they're worth pinning.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest

import qrz

_NS = 'xmlns="http://xmldata.qrz.com"'

_AUTH_OK       = f'<QRZDatabase {_NS}><Session><Key>NEWKEY123</Key><Count>1</Count></Session></QRZDatabase>'
_AUTH_ERR_E    = f'<QRZDatabase {_NS}><Session><e>Username/password incorrect</e></Session></QRZDatabase>'
_LOOKUP_OK     = (f'<QRZDatabase {_NS}><Callsign><call>K2BR</call><fname>Test</fname>'
                  f'<name>Club</name><grid>FM29</grid></Callsign>'
                  f'<Session><Key>NEWKEY123</Key></Session></QRZDatabase>')
_LOOKUP_EXPIRED = f'<QRZDatabase {_NS}><Session><Error>Session Timeout</Error></Session></QRZDatabase>'
_LOOKUP_NOCALL  = f'<QRZDatabase {_NS}><Session><Key>NEWKEY123</Key></Session></QRZDatabase>'


class _Resp:
    def __init__(self, text): self._text, self.status = text, 200
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def text(self): return self._text


class _Session:
    """One fake session whose queue persists across the client's repeated
    `async with aiohttp.ClientSession()` blocks (auth, lookup, re-auth…)."""
    def __init__(self, queue): self._q = queue
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def get(self, url, params=None, timeout=None): return _Resp(self._q.pop(0))


def _patch(monkeypatch, *texts):
    session = _Session(list(texts))
    monkeypatch.setattr(qrz.aiohttp, "ClientSession", lambda *a, **k: session)


def _run(coro):
    return asyncio.run(coro)


# ── parse helpers ─────────────────────────────────────────────────────────────

def test_find_resolves_namespaced_then_falls_back():
    ns_doc = ET.fromstring(f'<QRZDatabase {_NS}><Session><Key>abc</Key></Session></QRZDatabase>')
    assert qrz._find(ns_doc, "Session/Key") == "abc"

    plain = ET.fromstring('<QRZDatabase><Session><Key>xyz</Key></Session></QRZDatabase>')
    assert qrz._find(plain, "Session/Key") == "xyz"    # no-namespace fallback

    assert qrz._find(ns_doc, "Session/Missing") is None


def test_parse_xml_raises_qrzerror_on_bad_xml():
    with pytest.raises(qrz.QRZError):
        qrz._parse_xml("definitely not <xml")


# ── lookup flow ───────────────────────────────────────────────────────────────

def test_lookup_authenticates_then_returns_fields(monkeypatch):
    _patch(monkeypatch, _AUTH_OK, _LOOKUP_OK)
    c = qrz.QRZClient("user", "key")
    result = _run(c.lookup("k2br"))
    assert result["callsign"] == "K2BR"
    assert result["grid"] == "FM29"
    assert c._session_key == "NEWKEY123"


def test_auth_error_reported_via_e_element(monkeypatch):
    _patch(monkeypatch, _AUTH_ERR_E)          # QRZ uses <e>, not <Error>
    c = qrz.QRZClient("user", "badkey")
    with pytest.raises(qrz.QRZError):
        _run(c.lookup("k2br"))


def test_expired_session_reauthenticates_once_then_succeeds(monkeypatch):
    _patch(monkeypatch, _LOOKUP_EXPIRED, _AUTH_OK, _LOOKUP_OK)
    c = qrz.QRZClient("user", "key")
    c._session_key = "STALE"                  # skip initial auth; first get is the lookup
    result = _run(c.lookup("k2br"))
    assert result["callsign"] == "K2BR"
    assert c._session_key == "NEWKEY123"      # re-acquired after the timeout


def test_callsign_not_found_raises(monkeypatch):
    _patch(monkeypatch, _LOOKUP_NOCALL)
    c = qrz.QRZClient("user", "key")
    c._session_key = "KEY"
    with pytest.raises(qrz.QRZError):
        _run(c.lookup("k2br"))
