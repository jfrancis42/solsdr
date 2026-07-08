#!/usr/bin/env python3
"""
Measure phase coherence between RX1 and RX2 on the SunSDR2 PRO.

Both receivers are tuned to the SAME frequency on a common signal (a strong,
steady carrier works best). With a single shared antenna/ADC feeding both DDCs,
this tells you whether the two digital downconverters hold a stable phase
relationship — the prerequisite for direction finding, beamforming, and
two-antenna noise cancelling.

Method (the RIGHT way — spectral, signal-selective):
  Capture IQ from both receivers via the Radio callback (index-tagged), then
  compute a Welch-averaged cross-spectrum and the magnitude-squared coherence
  gamma^2(f) = |Sxy|^2 / (Sxx*Syy) PER FREQUENCY BIN. Report gamma^2 in the
  strongest signal bins (not the whole band — averaging whole-band power on a
  weak/bursty signal is dominated by uncorrelated noise and badly understates
  coherence). gamma^2 ~1.0 at the signal = phase-locked; ~0 = incoherent.

Runs the radio directly (no root, no wire capture). Uses the 2-arg stream
callback to get both receivers' IQ tagged by index.

Usage:
    python3 tools/rx2_coherence.py --freq 14074 --seconds 8
    python3 tools/rx2_coherence.py --freq 14074 --rx2-freq 14074   # explicit
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from solsdr.radio import Radio


def spectral_coherence(a, b, nfft=4096):
    """Welch-averaged magnitude-squared coherence per bin, plus signal-weighted
    summary. Returns (gamma2_array, power_array)."""
    w = np.hanning(nfft)
    m = min(len(a), len(b))
    segs = m // nfft
    Sxy = np.zeros(nfft, dtype=complex)
    Sxx = np.zeros(nfft)
    Syy = np.zeros(nfft)
    for k in range(segs):
        sa = a[k * nfft:(k + 1) * nfft] * w
        sb = b[k * nfft:(k + 1) * nfft] * w
        Fa = np.fft.fft(sa)
        Fb = np.fft.fft(sb)
        Sxy += Fa * np.conj(Fb)
        Sxx += np.abs(Fa) ** 2
        Syy += np.abs(Fb) ** 2
    gamma2 = (np.abs(Sxy) ** 2) / (Sxx * Syy + 1e-30)
    return gamma2, (Sxx + Syy), Sxy, segs


def capture(freq_hz, seconds, rate=None, local_ip='10.1.2.185',
            radio_ip='10.1.2.3', rx2_freq=None):
    r = Radio(radio_ip=radio_ip, local_ip=local_ip, variant='PRO',
              rx2=True, verbose=False, sample_rate=rate)
    if not r.open(wake_timeout=20):
        raise RuntimeError('radio open failed')
    r.set_frequency(freq_hz)
    r.set_frequency(rx2_freq if rx2_freq else freq_hz, rx=1)
    time.sleep(0.5)
    rx1, rx2 = [], []

    def cb(idx, iq):
        (rx1 if idx == 0 else rx2).append(iq.copy())
    r.start_stream(cb)
    time.sleep(seconds)
    r.close()
    n = min(len(rx1), len(rx2))
    a = np.concatenate(rx1[:n]) if n else np.zeros(0, np.complex64)
    b = np.concatenate(rx2[:n]) if n else np.zeros(0, np.complex64)
    m = min(len(a), len(b))
    return r.wire_rate, a[:m], b[:m]


def report(rate, a, b):
    if len(a) < 8192:
        print('not enough IQ captured'); return None
    pa = np.mean(np.abs(a) ** 2)
    g2, power, Sxy, segs = spectral_coherence(a, b)
    thresh = np.percentile(power, 99)
    sig = power >= thresh
    pk = int(np.argmax(power))
    g_sig = float(np.mean(g2[sig]))
    print(f'  samples={len(a)} segs={segs} '
          f'RX1={10*np.log10(pa+1e-30):.1f} dBFS')
    print(f'  gamma^2 @ strongest bins (top1%): {g_sig:.3f}  '
          f'(peak bin {g2[pk]:.3f})')
    print(f'  gamma^2 across all bins:          {float(np.mean(g2)):.3f}')
    print(f'  peak-bin cross-phase:             {np.degrees(np.angle(Sxy[pk])):+.1f} deg')
    verdict = ('STRONGLY COHERENT' if g_sig > 0.9 else
               'COHERENT' if g_sig > 0.6 else 'NOT coherent at the signal')
    print(f'  VERDICT: {verdict}')
    return g_sig, np.degrees(np.angle(Sxy[pk]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--freq', type=float, default=14074.0,
                    help='kHz; RX1 (and RX2 unless --rx2-freq) tuned here. Use a '
                         'strong steady signal (busy 20 m FT8, a broadcast carrier).')
    ap.add_argument('--rx2-freq', type=float, default=None,
                    help='kHz for RX2 if you want to test coherence with the two '
                         'receivers on DIFFERENT frequencies (default: same as --freq)')
    ap.add_argument('--seconds', type=float, default=8.0)
    ap.add_argument('--rate', type=float, default=None)
    ap.add_argument('--local-ip', default='10.1.2.185')
    ap.add_argument('--radio-ip', default='10.1.2.3')
    ap.add_argument('--repeat', type=int, default=1,
                    help='capture N times (restart the stream each time) to check '
                         'whether the fixed phase offset is repeatable across runs')
    args = ap.parse_args()

    f1 = int(args.freq * 1000)
    f2 = int(args.rx2_freq * 1000) if args.rx2_freq else None
    print(f'RX1={args.freq} kHz  RX2={args.rx2_freq or args.freq} kHz  '
          f'{args.seconds:.0f}s x{args.repeat}')
    offsets = []
    for i in range(args.repeat):
        print(f'--- run {i + 1}/{args.repeat} ---')
        rate, a, b = capture(f1, args.seconds, rate=args.rate,
                             local_ip=args.local_ip, radio_ip=args.radio_ip,
                             rx2_freq=f2)
        res = report(rate, a, b)
        if res:
            offsets.append(res[1])
    if len(offsets) > 1:
        spread = max(offsets) - min(offsets)
        print(f'\nfixed-offset repeatability across {len(offsets)} runs: '
              f'{offsets} deg  (spread {spread:.1f} deg)')
        print('  -> stable offset = one-time calibration; varies = per-session cal'
              if spread < 30 else
              '  -> offset varies per run: calibrate against a common signal each session')


if __name__ == '__main__':
    sys.exit(main())
