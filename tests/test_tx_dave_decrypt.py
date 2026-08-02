"""
Inbound DAVE (E2EE) decryption for TX (Discord → repeater).

Discord made DAVE end-to-end voice encryption mandatory on 2026-03-01, so every
inbound voice frame arrives as MLS ciphertext and opus_decode fails on all of
them (upstream discord-ext-voice-recv issue #53). _dave_decrypt() reuses the
DaveSession discord.py already maintains to decrypt each frame in place before
decode — a port of voice_recv PR #58.

These exercise the decrypt logic with fakes: no real davey, voice_recv, or
Discord voice connection required, so they run anywhere CI does.
"""
from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest


class _FakeDavey:
    # Only davey.MediaType.audio is touched on the hot path.
    MediaType = SimpleNamespace(audio=0)


def _make(*, ready=True, version=1, cached_id=4242, cipher=b"CIPHER"):
    """A fake PacketDecoder + packet wired like the real ones _dave_decrypt reads."""
    calls: list[tuple] = []

    def _decrypt(uid, media_type, data):
        calls.append((uid, media_type, data))
        return b"PLAINopus"

    session = SimpleNamespace(ready=ready, decrypt=_decrypt)
    conn    = SimpleNamespace(dave_session=session, dave_protocol_version=version)
    vc      = SimpleNamespace(_connection=conn)
    sink    = SimpleNamespace(voice_client=vc)
    decoder = SimpleNamespace(sink=sink, ssrc=99, _cached_id=cached_id)
    packet  = SimpleNamespace(decrypted_data=cipher)
    return decoder, packet, session, calls


def test_decrypts_frame_in_place(bot_module):
    decoder, packet, _session, calls = _make()
    bot_module._dave_decrypt(decoder, packet, _FakeDavey)
    assert packet.decrypted_data == b"PLAINopus"
    # decrypt() gets (int user_id, MediaType.audio, the original ciphertext bytes)
    assert calls == [(4242, 0, b"CIPHER")]


def test_noop_when_session_not_ready(bot_module):
    decoder, packet, _session, calls = _make(ready=False)
    bot_module._dave_decrypt(decoder, packet, _FakeDavey)
    assert packet.decrypted_data == b"CIPHER"
    assert calls == []


def test_noop_when_protocol_version_zero(bot_module):
    decoder, packet, _session, calls = _make(version=0)
    bot_module._dave_decrypt(decoder, packet, _FakeDavey)
    assert packet.decrypted_data == b"CIPHER"
    assert calls == []


def test_noop_when_ssrc_unmapped(bot_module):
    # No sender yet → can't pick a ratchet → leave the frame for normal handling.
    decoder, packet, _session, calls = _make(cached_id=None)
    bot_module._dave_decrypt(decoder, packet, _FakeDavey)
    assert packet.decrypted_data == b"CIPHER"
    assert calls == []


def test_noop_when_no_data(bot_module):
    decoder, packet, _session, calls = _make(cipher=b"")
    bot_module._dave_decrypt(decoder, packet, _FakeDavey)
    assert packet.decrypted_data == b""
    assert calls == []


def test_passthrough_frame_left_unchanged_on_decrypt_error(bot_module):
    # Silence/keepalive frames arrive unencrypted; decrypt raises and the frame
    # must be left untouched so it decodes fine as-is.
    decoder, packet, session, _calls = _make()

    def _raise(uid, media_type, data):
        raise ValueError("UnencryptedWhenPassthroughDisabled")

    session.decrypt = _raise
    bot_module._dave_decrypt(decoder, packet, _FakeDavey)
    assert packet.decrypted_data == b"CIPHER"


def test_enable_respects_idempotency_guard(bot_module, monkeypatch):
    # With the guard already set, enabling must early-return without importing
    # or patching anything.
    monkeypatch.setattr(bot_module, "_vr_dave_decrypt_enabled", True)
    bot_module._enable_dave_receive_decrypt()   # must not raise
    assert bot_module._vr_dave_decrypt_enabled is True


def test_enable_patches_process_packet_when_available(bot_module, monkeypatch):
    # When voice_recv + davey are installed (as on CI via discord.py[voice]),
    # enabling replaces PacketDecoder._process_packet. Restore it afterward so
    # the global class isn't left patched for other tests.
    if not (
        importlib.util.find_spec("discord.ext.voice_recv")
        and importlib.util.find_spec("davey")
    ):
        pytest.skip("voice_recv/davey not installed")

    from discord.ext.voice_recv.opus import PacketDecoder

    orig = PacketDecoder._process_packet
    monkeypatch.setattr(bot_module, "_vr_dave_decrypt_enabled", False)
    try:
        bot_module._enable_dave_receive_decrypt()
        assert PacketDecoder._process_packet is not orig
        assert bot_module._vr_dave_decrypt_enabled is True
    finally:
        PacketDecoder._process_packet = orig
