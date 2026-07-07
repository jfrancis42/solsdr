#!/usr/bin/env python3
"""Stateful RX DSP filter tests. Offline, no radio."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from solsdr.dsp.filters import (IQChannelFilter, NotchFilter, AudioPeakFilter,
                                 NoiseBlanker, Squelch, RXFilterChain)

FS = 48000


def _energy(x, f):
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    fr = np.fft.rfftfreq(len(x), 1 / FS)
    return sp[np.argmin(np.abs(fr - f))]


def test_notch_cuts_interferer():
    t = np.arange(FS) / FS
    sig = 0.3 * np.sin(2 * np.pi * 600 * t)
    interf = 0.5 * np.sin(2 * np.pi * 1000 * t)
    nf = NotchFilter(FS, 1000, bandwidth_hz=50)
    out = nf.process((sig + interf).astype(np.float64))
    assert _energy(out, 1000) < _energy(sig + interf, 1000) * 0.3
    assert _energy(out, 600) > _energy(sig + interf, 600) * 0.7
    print('PASS notch cuts interferer, preserves signal')


def test_apf_narrows():
    t = np.arange(FS) / FS
    x = (0.3 * np.sin(2 * np.pi * 600 * t) + 0.5 * np.sin(2 * np.pi * 1500 * t))
    apf = AudioPeakFilter(FS, 600, level=0.9)
    out = apf.process(x.astype(np.float64))
    assert _energy(out, 600) > _energy(out, 1500) * 3
    print('PASS audio peak filter isolates the CW pitch')


def test_squelch_gates():
    t = np.arange(FS) / FS
    sq = Squelch(0.5)
    loud = sq.process((0.3 * np.sin(2 * np.pi * 600 * t)).astype(np.float64))
    quiet = sq.process((0.001 * np.random.randn(FS)).astype(np.float64))
    assert np.max(np.abs(loud)) > 0 and np.max(np.abs(quiet)) == 0
    print('PASS squelch passes signal, mutes noise')


def test_noise_blanker_removes_impulse():
    t = np.arange(FS) / FS
    x = (0.2 * np.sin(2 * np.pi * 600 * t)).astype(np.float64)
    x[1000] = 5.0  # impulse
    nb = NoiseBlanker(0.8)
    out = nb.process(x)
    assert abs(out[1000]) < 1.0, 'impulse not blanked'
    print('PASS noise blanker removes impulse')


def test_iq_channel_filter_runs_stateful():
    iqf = IQChannelFilter(39062.5, 3000)
    a = iqf.process((np.random.randn(2000) + 1j * np.random.randn(2000)).astype(np.complex64))
    b = iqf.process((np.random.randn(2000) + 1j * np.random.randn(2000)).astype(np.complex64))
    assert len(a) == 2000 and len(b) == 2000 and a.dtype == np.complex64
    print('PASS IQ channel filter runs stateful')


def test_chain_all_off_is_passthrough():
    chain = RXFilterChain(FS)
    x = (0.2 * np.random.randn(2000)).astype(np.float32)
    out = chain.process(x)
    assert np.allclose(out, x, atol=1e-6), 'all-off chain must be transparent'
    print('PASS filter chain is transparent when all stages off')


if __name__ == '__main__':
    test_notch_cuts_interferer()
    test_apf_narrows()
    test_squelch_gates()
    test_noise_blanker_removes_impulse()
    test_iq_channel_filter_runs_stateful()
    test_chain_all_off_is_passthrough()
    print('\nFILTER TESTS PASSED')
