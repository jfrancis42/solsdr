#!/usr/bin/env python3
"""
Offline DSP optimization: decode saved raw-IQ cycles with a given Demodulator
config and report FT8 decode counts. Runs on greybox (has jt9).

Because it re-decodes the SAME captured IQ, differences in decode count reflect
DSP quality, not band variability. Use it to sweep filter/AGC/resample choices.

Usage:
    python3 tools/offline_decode.py --iqdir /tmp/iqcap_local [--mode USB]
"""
import argparse
import glob
import os
import subprocess
import sys
import wave

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.dsp.demod import Demodulator

JT9 = '/usr/bin/jt9'
FT8_AUDIO_RATE = 12000


def demod_to_wav(iq, wire_rate, wav_path, mode='USB', **demod_kw):
    d = Demodulator(wire_rate=wire_rate, audio_rate=FT8_AUDIO_RATE, mode=mode)
    for k, v in demod_kw.items():
        setattr(d, k, v)
    # process in chunks like the live path
    out = []
    for i in range(0, len(iq) - 2000, 2000):
        out.append(d.process(iq[i:i + 2000]))
    audio = np.concatenate(out) if out else np.zeros(1)
    peak = np.max(np.abs(audio)) or 1.0
    pcm16 = (np.clip(audio / peak * 0.9, -1, 1) * 32767).astype(np.int16)
    with wave.open(wav_path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(FT8_AUDIO_RATE)
        w.writeframes(pcm16.tobytes())
    return audio


def jt9_decode(wav_path):
    p = subprocess.run([JT9, '-8', '-a', '/tmp', '-t', '/tmp', wav_path],
                       capture_output=True, text=True, timeout=60)
    lines = [l for l in (p.stdout + p.stderr).splitlines()
             if l.strip() and not l.startswith('<Decode')]
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--iqdir', default='/tmp/iqcap_local')
    ap.add_argument('--mode', default='USB')
    ap.add_argument('--label', default='baseline')
    args = ap.parse_args()

    wr_file = os.path.join(args.iqdir, 'wire_rate.txt')
    wire_rate = float(open(wr_file).read().strip()) if os.path.exists(wr_file) else 39062.5

    total = 0
    calls = set()
    for npy in sorted(glob.glob(os.path.join(args.iqdir, 'iq_cycle*.npy'))):
        iq = np.load(npy)
        wav = f'/tmp/off_{os.path.basename(npy)}.wav'
        demod_to_wav(iq, wire_rate, wav, mode=args.mode)
        lines = jt9_decode(wav)
        total += len(lines)
        for l in lines:
            # crude call extraction: tokens after the '~'
            if '~' in l:
                parts = l.split('~')[1].split()
                calls.update(p for p in parts if any(c.isdigit() for c in p) and len(p) >= 3)
    print(f'[{args.label}] mode={args.mode} decodes={total} unique_tokens={len(calls)}')
    return total


if __name__ == '__main__':
    main()
