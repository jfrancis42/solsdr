#!/usr/bin/env python3
"""
Capture raw complex64 IQ from the radio to a .npy file, aligned to the FT8
cycle. Lets DSP be optimized offline against fixed data (isolating DSP quality
from band variability) and re-decoded repeatedly.

Usage:
    python3 tools/capture_iq.py [freq_khz] --cycles N --outdir DIR
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio

FT8_PERIOD = 15


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('freq_khz', nargs='?', type=float, default=14074.0)
    ap.add_argument('--cycles', type=int, default=1)
    ap.add_argument('--outdir', default='/tmp/iqcap')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    for f in os.listdir(args.outdir):
        if f.endswith('.npy'):
            os.remove(os.path.join(args.outdir, f))

    radio = Radio(radio_ip='10.1.2.3', local_ip='10.1.2.185', variant='PRO',
                  verbose=True)
    buf = {'iq': [], 'on': False}

    def on_iq(iq):
        if buf['on']:
            buf['iq'].append(iq)

    if not radio.open():
        print('radio open failed'); sys.exit(1)
    radio.set_frequency(int(args.freq_khz * 1000))
    radio.start_stream(on_iq)
    time.sleep(1.0)

    try:
        for c in range(args.cycles):
            wait = FT8_PERIOD - (time.time() % FT8_PERIOD)
            time.sleep(wait)
            buf['iq'] = []
            buf['on'] = True
            time.sleep(FT8_PERIOD + 0.5)
            buf['on'] = False
            if not buf['iq']:
                print(f'cycle {c+1}: no IQ'); continue
            iq = np.concatenate(buf['iq']).astype(np.complex64)
            path = os.path.join(args.outdir, f'iq_cycle{c+1}.npy')
            np.save(path, iq)
            print(f'cycle {c+1}: {len(iq)} samples '
                  f'({len(iq)/radio.wire_rate:.1f}s) -> {path}')
        # record the wire rate alongside for the offline decoder
        with open(os.path.join(args.outdir, 'wire_rate.txt'), 'w') as f:
            f.write(str(radio.wire_rate))
    finally:
        radio.close()


if __name__ == '__main__':
    main()
