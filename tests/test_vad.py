"""Energy-threshold VAD state machine (_update_vad) and transmission recording.

Covers the decision logic the buffer tests in test_audio_client.py don't touch:
the speaking-True start edge, the hangover countdown (and word-gap tolerance),
the end edge that reports a completed transmission, the VAD_MIN_TX_SECONDS
duration filter, and the recording buffer (cap + WAV encode). These are the
subtlest parts of the audio path and have had real bugs (stuck speaking
indicator, over-eager fragmentation), so they're worth pinning.
"""
from __future__ import annotations

import io
import struct
import wave

import repeater_audio as ra

_SAMPLES = ra.RTP_FRAME_BYTES // 2   # 160 samples per 20ms 8kHz mono frame


def _loud(amp: int = 8000) -> bytes:
    """A frame whose RMS is well above any sane threshold."""
    return struct.pack("<%dh" % _SAMPLES, *([amp] * _SAMPLES))


_SILENT = bytes(ra.RTP_FRAME_BYTES)   # all-zero samples → RMS 0


def _vad_client(threshold=400, hangover_s=0.06, record=False, max_rec_s=300.0):
    """A RepeaterAudioClient with only the VAD fields set — no SIP, no threads."""
    c = ra.RepeaterAudioClient.__new__(ra.RepeaterAudioClient)
    c.extension = "50420"
    c._vad_rms_threshold = threshold
    c._vad_hangover_seconds = hangover_s
    c._vad_hangover_frames = max(1, round(hangover_s * 1000 / ra.FRAME_MS))
    c._voice_active = False
    c._silence_run = 0
    c._activity_start_ts = None
    c._on_transmission = None
    c._on_speaking_change = None
    c._pause_pending = False
    c.record_transmissions = record
    c._recording_frames = []
    c.max_recording_seconds = max_rec_s
    return c


def _fixed_clock(monkeypatch, holder):
    monkeypatch.setattr(ra.time, "time", lambda: holder["t"])


def test_start_edge_fires_speaking_true_once():
    edges = []
    c = _vad_client()
    c._on_speaking_change = lambda active: edges.append(active)
    c._update_vad(_loud())
    assert edges == [True] and c._voice_active is True
    c._update_vad(_loud())            # still loud → no repeat edge
    assert edges == [True]


def test_end_edge_reports_transmission_with_hangover_subtracted(monkeypatch):
    clock = {"t": 1000.0}
    _fixed_clock(monkeypatch, clock)
    tx, spk = [], []
    c = _vad_client(hangover_s=0.06)   # 3 hangover frames
    c._on_transmission = lambda d, rec: tx.append((d, rec))
    c._on_speaking_change = lambda a: spk.append(a)

    c._update_vad(_loud())             # start at t=1000
    clock["t"] = 1002.0                # 2s of activity, then silence
    c._update_vad(_SILENT)             # silence_run 1
    c._update_vad(_SILENT)             # silence_run 2
    assert tx == []                    # still within hangover
    c._update_vad(_SILENT)             # silence_run 3 == hangover → end

    assert spk == [True, False]
    assert c._pause_pending is True and c._voice_active is False
    assert len(tx) == 1
    duration, recording = tx[0]
    assert abs(duration - (2.0 - 0.06)) < 1e-6   # voice time minus the hangover
    assert recording is None                     # recording disabled


def test_short_blip_still_blinks_indicator_but_is_not_reported(monkeypatch):
    clock = {"t": 500.0}
    _fixed_clock(monkeypatch, clock)
    tx, spk = [], []
    c = _vad_client(hangover_s=0.06)
    c._on_transmission = lambda d, rec: tx.append(d)
    c._on_speaking_change = lambda a: spk.append(a)

    c._update_vad(_loud())             # start
    clock["t"] = 500.2                 # only 0.2s of activity
    for _ in range(3):
        c._update_vad(_SILENT)

    # duration 0.2 - 0.06 = 0.14 < VAD_MIN_TX_SECONDS → not reported…
    assert tx == []
    # …but the speaking indicator still went on and off.
    assert spk == [True, False] and c._pause_pending is True


def test_hangover_tolerates_a_word_gap():
    c = _vad_client(hangover_s=0.06)   # 3 hangover frames
    reported = []
    c._on_transmission = lambda d, rec: reported.append(d)
    c._update_vad(_loud())             # start
    c._update_vad(_SILENT)             # gap 1
    c._update_vad(_SILENT)             # gap 2 (still < 3)
    c._update_vad(_loud())             # talking resumes → gap resets
    assert c._voice_active is True and c._silence_run == 0
    assert reported == []              # transmission never ended


def test_recording_frame_buffer_is_capped():
    c = _vad_client(record=True, max_rec_s=0.1)   # 0.1s → 5 frames of 20ms
    for _ in range(25):
        c._append_recording_frame(_SILENT)
    assert len(c._recording_frames) == 5


def test_encode_recording_wav_shape_and_empty_case():
    c = _vad_client(record=True)
    c._recording_frames = [_loud(), _loud()]
    blob = c._encode_recording_wav()
    assert blob is not None
    with wave.open(io.BytesIO(blob), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == ra.RTP_SAMPLE_RATE
        assert wf.getnframes() == _SAMPLES * 2

    c._recording_frames = []
    assert c._encode_recording_wav() is None    # nothing buffered → None


def test_recording_attached_to_transmission_when_enabled(monkeypatch):
    clock = {"t": 0.0}
    _fixed_clock(monkeypatch, clock)
    attached = []
    c = _vad_client(hangover_s=0.02, record=True)   # 1 hangover frame
    c._on_transmission = lambda d, rec: attached.append(rec)

    c._update_vad(_loud())             # start → recording reset + this frame kept
    clock["t"] = 1.0
    c._update_vad(_SILENT)             # 1 silent frame == hangover → end + encode

    assert len(attached) == 1 and attached[0] is not None   # WAV bytes attached
    with wave.open(io.BytesIO(attached[0]), "rb") as wf:
        assert wf.getnframes() == _SAMPLES * 2              # loud + trailing silent
