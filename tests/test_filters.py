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


def test_demod_adjustable_passband():
    """The demod filter passband is RF offsets from the dial. Narrowing it
    should attenuate a tone that was inside the wide band, and set_mode resets
    the passband to the mode default."""
    from solsdr.dsp.demod import Demodulator
    WR = 39062.5

    def usb_tone_level(dem, rf_offset_hz):
        # A USB signal at RF offset `rf_offset_hz` above the dial. The SunSDR2
        # wire IQ is sideband-mirrored and the demod conjugates, so a real +offset
        # signal arrives at NEGATIVE baseband: inject exp(-j2*pi*offset*t). Longer
        # block so the sharp FIR fully warms up before measuring.
        n = 60000
        t = np.arange(n) / WR
        iq = np.exp(-2j * np.pi * rf_offset_hz * t).astype(np.complex64)
        dem.process(iq[:n // 2])                 # prime filter state
        out = dem.process(iq[n // 2:])
        return float(np.sqrt(np.mean(out ** 2)) + 1e-12)

    # defaults per mode (SSB inner edge is 100 Hz)
    assert Demodulator(mode='USB').filter_lo == 100
    assert Demodulator(mode='LSB').filter_hi == -100

    # a 2000 Hz USB tone passes the default 300..2700 band but is rejected by a
    # narrow 300..1000 band. Unity gain (fixed:1) so the fixed_gain*clip doesn't
    # saturate both to 0.98 and mask the filtering.
    wide = usb_tone_level(Demodulator(mode='USB', agc='fixed:1'), 2000.0)
    d = Demodulator(mode='USB', agc='fixed:1'); d.set_filter(300, 1000)
    narrow = usb_tone_level(d, 2000.0)
    assert narrow < wide * 0.2, f'narrow band did not reject 2 kHz tone ({narrow:.4f} vs {wide:.4f})'

    # set_mode resets the passband away from a custom setting
    d.set_mode('CW')
    assert d.filter_lo < 0 < d.filter_hi, 'CW passband should straddle 0'
    print('PASS demod adjustable passband (narrowing rejects out-of-band tone)')


def test_ssb_image_rejection():
    """USB/LSB must reject the OPPOSITE sideband (single-sideband, not double).

    This guards the double-sideband bug: taking iq.real folds -f onto +f, so a
    station on one side of the dial aliased onto the other. The fixed demod
    selects ONE sideband and rejects the image.

    Sign note: the SunSDR2 RX IQ is sideband-MIRRORED on the wire, so the demod
    conjugates. Net effect in these synthetic tests (tone at exp(+j2*pi*f*t)):
    USB selects NEGATIVE baseband, LSB selects POSITIVE. What matters for this
    test is that each mode passes ONE side and rejects the other by >30 dB.

    Measured at UNITY gain (fixed:1) — the default fixed_gain (3000x) clips both
    to full scale and would mask the rejection (this exact measurement trap hid
    the bug during dev)."""
    from solsdr.dsp.demod import Demodulator
    WR = 39062.5

    def level(offset_hz, mode='USB'):
        d = Demodulator(mode=mode, agc='fixed:1')
        n = 60000
        t = np.arange(n) / WR
        iq = (0.01 * np.exp(2j * np.pi * offset_hz * t)).astype(np.complex64)
        d.process(iq[:n // 2])                 # prime filter/mixer state
        out = d.process(iq[n // 2:])
        return float(np.sqrt(np.mean(out ** 2)) + 1e-15)

    # USB passes one baseband sign and rejects the other; LSB is the mirror.
    # (After the wire-mirror conjugation, USB=negative baseband, LSB=positive.)
    usb_pass, usb_img = level(-1500, 'USB'), level(1500, 'USB')
    lsb_pass, lsb_img = level(1500, 'LSB'), level(-1500, 'LSB')
    urej = 20 * np.log10(usb_pass / usb_img)
    lrej = 20 * np.log10(lsb_pass / lsb_img)
    assert urej > 30, f'USB image rejection only {urej:.1f} dB (want >30)'
    assert lrej > 30, f'LSB image rejection only {lrej:.1f} dB (want >30)'
    # USB and LSB must pick OPPOSITE sides (not the same one)
    assert (usb_pass > usb_img) and (lsb_pass > lsb_img)
    print(f'PASS SSB image rejection (USB {urej:.0f} dB, LSB {lrej:.0f} dB, '
          f'opposite sidebands)')


def test_ssb_filter_sharpness():
    """Selectable SSB skirt sharpness: sharper profiles must attenuate a signal
    just OUTSIDE the passband edge more, and pass the in-band signal equally.
    Guards the 'gentle filter leaks adjacent signals' bug."""
    from solsdr.dsp.demod import Demodulator
    WR = 39062.5

    def resp(sharp, rf_offset):
        d = Demodulator(mode='USB', agc='fixed:1', filter_sharpness=sharp)
        n = 60000
        t = np.arange(n) / WR
        iq = np.exp(-2j * np.pi * rf_offset * t).astype(np.complex64)
        d.process(iq[:n // 2]); out = d.process(iq[n // 2:])
        return float(np.sqrt(np.mean(out ** 2)) + 1e-15)

    # in-band (1500 Hz, inside 300..2700) passes at ~full level for all profiles
    inband = {s: resp(s, 1500) for s in ('soft', 'normal', 'sharp')}
    assert min(inband.values()) > 0.5 * max(inband.values()), inband

    # 300 Hz outside the edge (+3000): sharper -> more attenuation
    a_soft = resp('soft', 3000)
    a_norm = resp('normal', 3000)
    a_sharp = resp('sharp', 3000)
    assert a_norm < a_soft, f'normal not sharper than soft ({a_norm} vs {a_soft})'
    assert a_sharp < a_norm, f'sharp not sharper than normal ({a_sharp} vs {a_norm})'
    # normal must actually reject the adjacent signal well (>40 dB down)
    rej = 20 * np.log10(resp('normal', 1500) / a_norm)
    assert rej > 40, f'normal only {rej:.0f} dB down 300 Hz outside edge'

    # runtime switch works
    d = Demodulator(mode='USB')
    assert d.set_sharpness('sharp') and d.filter_sharpness == 'sharp'
    assert not d.set_sharpness('bogus') and d.filter_sharpness == 'sharp'
    print(f'PASS SSB filter sharpness (normal {rej:.0f} dB down at edge+300)')


if __name__ == '__main__':
    test_notch_cuts_interferer()
    test_apf_narrows()
    test_squelch_gates()
    test_noise_blanker_removes_impulse()
    test_iq_channel_filter_runs_stateful()
    test_chain_all_off_is_passthrough()
    test_demod_adjustable_passband()
    test_ssb_image_rejection()
    test_ssb_filter_sharpness()
    print('\nFILTER TESTS PASSED')
