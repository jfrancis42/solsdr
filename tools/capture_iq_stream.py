#!/usr/bin/env python3
"""
Capture a continuous block of RX IQ to a single self-describing file.

The file is written in solsdr's on-the-wire IQ format: one text header line
identical to the IQ server's, then the raw little-endian interleaved float32
I,Q stream (numpy complex64):

    SOLSDR IQ rate=39062.5 fmt=complex64 freq=14074000\n<complex64 samples...>

Because it's byte-for-byte what the IQ server emits, any client that reads the
server (the panadapter's file mode, GNU Radio via a small skip, etc.) can replay
the file with no extra metadata. Use it to make a fixed demo/example recording.

Usage (run on the host wired to the radio):
    python3 tools/capture_iq_stream.py 14074 --seconds 300 --out example.iqz
"""
import argparse
import os
import sys
import time

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio


def main():
    ap = argparse.ArgumentParser(description="capture continuous RX IQ to a "
                                             "wire-format file")
    ap.add_argument('freq_khz', nargs='?', type=float, default=14074.0)
    ap.add_argument('--seconds', type=float, default=300.0,
                    help='capture duration in seconds (default 300)')
    ap.add_argument('--out', default='/tmp/solsdr_example.iq',
                    help='output file path')
    ap.add_argument('--radio-ip', default='10.1.2.3')
    ap.add_argument('--local-ip', default='10.1.2.185')
    ap.add_argument('--variant', default='PRO', choices=['PRO', 'DX'])
    ap.add_argument('--rate', type=float, default=None,
                    help='IQ sample rate (default: radio default 39062.5)')
    args = ap.parse_args()

    radio = Radio(radio_ip=args.radio_ip, local_ip=args.local_ip,
                  variant=args.variant, sample_rate=args.rate, verbose=True)
    if not radio.open():
        print('radio open failed', file=sys.stderr)
        sys.exit(1)
    freq_hz = int(args.freq_khz * 1000)
    radio.set_frequency(freq_hz)
    rate = radio.wire_rate

    # Open the output and write the wire header first, so the file self-describes.
    f = open(args.out, 'wb')
    header = f'SOLSDR IQ rate={rate} fmt=complex64 freq={freq_hz}\n'
    f.write(header.encode('ascii'))

    state = {'on': False, 'bytes': 0, 'samples': 0}

    def on_iq(iq):
        if state['on']:
            b = np.ascontiguousarray(iq, dtype=np.complex64).tobytes()
            f.write(b)
            state['bytes'] += len(b)
            state['samples'] += len(iq)

    radio.start_stream(on_iq)
    time.sleep(1.0)                    # let the stream settle before recording

    target = int(rate * args.seconds)
    print(f'capturing {args.seconds:.0f}s @ {rate:.1f} S/s from '
          f'{args.freq_khz:.0f} kHz -> {args.out}')
    print(f'  (~{target * 8 / 1e6:.0f} MB expected)')
    state['on'] = True
    t0 = time.time()
    try:
        while state['samples'] < target:
            time.sleep(1.0)
            el = time.time() - t0
            print(f'  {el:5.0f}s  {state["samples"]:>11d} samples  '
                  f'{state["bytes"]/1e6:6.1f} MB', flush=True)
            if el > args.seconds + 30:      # safety: don't run forever on a stall
                print('  timeout guard hit — stopping', file=sys.stderr)
                break
    finally:
        state['on'] = False
        time.sleep(0.3)
        f.close()
        radio.close()

    dur = state['samples'] / rate if rate else 0
    print(f'done: {state["samples"]} samples ({dur:.1f}s effective), '
          f'{state["bytes"]/1e6:.1f} MB -> {args.out}')


if __name__ == '__main__':
    main()
