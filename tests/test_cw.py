#!/usr/bin/env python3
"""
CW encoder/decoder + full-chain RX self-validation. Offline, no radio.

  * encoder<->decoder round-trip at several speeds (standard timing)
  * Farnsworth timing math (word speed scales per PARIS standard)
  * FULL RX chain: keyed carrier -> BFO CW demod -> Morse decode == source text
    (this is the objective CW RX validation, analogous to the FT8 jt9 loop)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from solsdr.dsp.cw_decode import CWEncoder, CWDecoder, wpm_to_dot_seconds
from solsdr.dsp.demod import Demodulator

FS = 48000
WR = 39062.5


def _decode_stream(dec, audio, block=2000):
    out = ''
    for i in range(0, len(audio), block):
        out += dec.process(audio[i:i + block])
    out += dec.flush()
    return out.strip()


def test_roundtrip_speeds():
    text = 'CQ TEST DE N0GQ K'
    for wpm in (12, 15, 20, 25, 30):
        enc = CWEncoder(sample_rate=FS, pitch=600, char_wpm=wpm)
        dec = CWDecoder(sample_rate=FS, pitch=600, wpm=wpm)
        out = _decode_stream(dec, enc.encode(text))
        assert out == text, f'{wpm}wpm: {out!r} != {text!r}'
    print('PASS encoder<->decoder round-trip 12-30 wpm')


def test_farnsworth_timing():
    # 'PARIS ' occupies 60/word_wpm seconds regardless of char speed.
    for cw, ww in ((20, 20), (20, 10), (18, 5)):
        enc = CWEncoder(sample_rate=FS, char_wpm=cw, word_wpm=ww)
        dur = len(enc.encode('PARIS PARIS')) / FS
        expected = 2 * (60.0 / ww)  # ~2 words (minus one overlap gap)
        # within 15% (the trailing word has no word-gap after it)
        assert abs(dur - expected) / expected < 0.2, (cw, ww, dur, expected)
    print('PASS Farnsworth word-speed timing (PARIS standard)')


def test_full_rx_chain():
    """Keyed carrier at DC -> BFO CW demod -> Morse decode == source."""
    text = 'CQ TEST DE N0GQ K'
    pitch = 600.0
    # keyed carrier envelope at the wire rate (operator tuned on-frequency)
    enc = CWEncoder(sample_rate=WR, pitch=1.0, char_wpm=18, amplitude=1.0)
    env = np.abs(enc.encode(text))
    rng = np.random.default_rng(1)
    iq = (env + 0.02 * rng.standard_normal(len(env))
          + 1j * 0.02 * rng.standard_normal(len(env))).astype(np.complex64)
    # default agc='auto' -> AGC on for CW (preserves keying)
    dem = Demodulator(wire_rate=WR, audio_rate=FS, mode='CWU',
                      cw_pitch=pitch, cw_bandwidth=200)
    rec = []
    for i in range(0, len(iq) - 2000, 2000):
        rec.append(dem.process(iq[i:i + 2000]))
    recovered = np.concatenate(rec)
    dec = CWDecoder(sample_rate=FS, pitch=pitch, wpm=18)
    out = _decode_stream(dec, recovered)
    assert out == text, f'full-chain: {out!r} != {text!r}'
    print(f'PASS full RX chain: keyed carrier -> BFO demod -> "{out}"')


if __name__ == '__main__':
    test_roundtrip_speeds()
    test_farnsworth_timing()
    test_full_rx_chain()
    print('\nCW TESTS PASSED')
