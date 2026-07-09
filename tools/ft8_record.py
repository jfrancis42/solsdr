#!/usr/bin/env python3
"""
Record FT8-cycle-aligned audio WAVs from the SunSDR2 (no decoder dependency).

Runs on the radio host (tardis). Writes 12 kHz mono WAVs aligned to the FT8
15-second cycle boundary, suitable for feeding to jt9 -8 (decoded elsewhere).

Usage:
    python3 tools/ft8_record.py [freq_khz] --cycles N --mode USB --outdir DIR
"""
import argparse
import os
import sys
import time
import wave

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio, PRO_WIRE_RATE
from solsdr.dsp.demod import Demodulator

FT8_AUDIO_RATE = 12000
FT8_PERIOD = 15


def write_wav(path, audio, rate=FT8_AUDIO_RATE):
    peak = np.max(np.abs(audio)) or 1.0
    pcm16 = (np.clip(audio / peak * 0.9, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('freq_khz', nargs='?', type=float, default=14074.0)
    ap.add_argument('--cycles', type=int, default=2)
    ap.add_argument('--mode', default='USB')
    ap.add_argument('--outdir', default='/tmp/ft8val')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    for f in os.listdir(args.outdir):
        if f.endswith('.wav'):
            os.remove(os.path.join(args.outdir, f))

    radio = Radio(radio_ip='10.1.2.3', local_ip='10.1.2.185', variant='PRO',
                  verbose=True)
    # FT8 wants LINEAR audio — AGC distorts the ~50 simultaneous tones and
    # lowers decode count (verified 57->63). Use agc='off'.
    demod = Demodulator(wire_rate=PRO_WIRE_RATE, audio_rate=FT8_AUDIO_RATE,
                        mode=args.mode, agc='off')

    buf = {'audio': [], 'on': False}

    def on_iq(iq):
        if buf['on']:
            a = demod.process(iq)
            if len(a):
                buf['audio'].append(a)

    if not radio.open():
        print('radio open failed'); sys.exit(1)
    radio.set_frequency(int(args.freq_khz * 1000))
    radio.start_stream(on_iq)
    time.sleep(1.0)

    try:
        for c in range(args.cycles):
            wait = FT8_PERIOD - (time.time() % FT8_PERIOD)
            time.sleep(wait)
            buf['audio'] = []
            buf['on'] = True
            time.sleep(FT8_PERIOD + 0.5)
            buf['on'] = False
            if not buf['audio']:
                print(f'cycle {c+1}: no audio'); continue
            audio = np.concatenate(buf['audio'])
            rms = np.sqrt(np.mean(audio ** 2))
            path = os.path.join(args.outdir, f'cycle{c+1}.wav')
            write_wav(path, audio)
            print(f'cycle {c+1}: {len(audio)} samples rms={rms:.4f} -> {path}')
    finally:
        radio.close()


if __name__ == '__main__':
    main()
