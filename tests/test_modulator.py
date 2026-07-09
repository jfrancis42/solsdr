#!/usr/bin/env python3
"""
Modulator tests: audio -> IQ -> audio round-trip and rate conversion.
Offline, no radio, no privilege.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from solsdr.dsp.modulator import Modulator
from solsdr.dsp.demod import Demodulator


def _tone(freq, fs, n):
    t = np.arange(n) / fs
    return 0.5 * np.sin(2 * np.pi * freq * t)


def test_roundtrip_usb_lsb():
    fs, wr = 48000, 39062.5
    audio = _tone(1500, fs, fs)  # 1 s, 1500 Hz
    for mode in ('USB', 'LSB'):
        mod = Modulator(audio_rate=fs, wire_rate=wr, mode=mode)
        iq = mod.process(audio)
        # rate conversion correct
        assert abs(len(iq) - len(audio) * wr / fs) < 5, (mode, len(iq))
        assert iq.dtype == np.complex64
        assert np.max(np.abs(iq)) <= 0.99, 'IQ must stay in range for packing'
        # demod back and confirm the tone frequency
        dem = Demodulator(wire_rate=wr, audio_rate=fs, mode=mode, agc='off')
        out = np.concatenate([dem.process(iq[i:i + 2000])
                              for i in range(0, len(iq) - 2000, 2000)])
        N = min(len(out), 16384)
        sp = np.abs(np.fft.rfft(out[:N] * np.hanning(N)))
        peak = np.fft.rfftfreq(N, 1 / fs)[np.argmax(sp)]
        assert abs(peak - 1500) < 30, f'{mode} recovered {peak} Hz'
    print('PASS USB/LSB modulate->demodulate round-trip (1500 Hz recovered)')


def test_loud_input_no_clip():
    """Loud input (peak > 1) must be leveled, not hard-clipped into harmonics.

    Measured on the IQ spectrum directly: a clean tone should stay a single
    line, not spawn harmonics from clipping.
    """
    fs, wr = 48000, 39062.5
    audio = 4.0 * _tone(1000, fs, fs)  # peak 4.0, well over full scale
    mod = Modulator(audio_rate=fs, wire_rate=wr, mode='USB')
    iq = mod.process(audio)
    assert np.max(np.abs(iq)) <= 0.99, 'IQ must stay in range'
    # A USB +1 kHz tone lands at -1000 Hz in the modulator IQ (the SunSDR2 TX
    # path re-inverts it to +1 kHz out the antenna — see modulator.process).
    # Check for clip harmonics around that fundamental regardless of sign.
    N = 16384
    sp = np.abs(np.fft.fftshift(np.fft.fft(iq[:N] * np.hanning(N))))
    fr = np.fft.fftshift(np.fft.fftfreq(N, 1 / wr))
    fund = sp[(np.abs(fr) > 900) & (np.abs(fr) < 1100)].sum()
    # harmonics of a clipped 1 kHz tone would land at 2k/3k etc. (either sign)
    harm = sp[(np.abs(fr) > 1500) & (np.abs(fr) < 6000)].sum() + 1e-9
    assert fund / harm > 10, f'loud input produced clip harmonics: {fund/harm:.1f}'
    print(f'PASS loud-input leveling (IQ fundamental/harmonic {fund/harm:.0f})')


if __name__ == '__main__':
    test_roundtrip_usb_lsb()
    test_loud_input_no_clip()
    print('\nMODULATOR TESTS PASSED')
