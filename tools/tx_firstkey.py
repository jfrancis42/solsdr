#!/usr/bin/env python3
"""
First key-up test — steady tune tone into a DUMMY LOAD (amp bypassed).

Keys the radio at a single, bounded drive byte for a fixed short duration with
the dead-man as a hard backup, sending a steady 600 Hz USB tone. Prints the
exact sequence. Read the wattmeter while it's keyed.

Usage:
    python3 tools/tx_firstkey.py --drive 20 --seconds 5 --freq 14074
"""
import argparse
import sys
import time

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio
from solsdr.tx_session import TXSession


def tone_iter(audio_rate=48000, hz=600.0, amp=0.5):
    block = audio_rate // 50
    phase = 0.0
    dphi = 2 * np.pi * hz / audio_rate
    while True:
        idx = np.arange(block)
        yield (amp * np.sin(phase + dphi * idx)).astype(np.float32)
        phase = (phase + dphi * block) % (2 * np.pi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--freq', type=float, default=14074.0, help='kHz')
    ap.add_argument('--drive', type=int, default=20, help='raw drive byte 0-255')
    ap.add_argument('--seconds', type=float, default=5.0, help='keydown time')
    ap.add_argument('--max-drive', type=int, default=255, help='hard drive ceiling')
    ap.add_argument('--tone', type=float, default=600.0)
    ap.add_argument('--amp', type=float, default=0.5, help='IQ tone amplitude 0-1')
    args = ap.parse_args()

    print('=' * 60)
    print('  FIRST KEY-UP TEST — steady tone into DUMMY LOAD')
    print(f'  freq {args.freq} kHz  drive byte {args.drive} (max {args.max_drive})')
    print(f'  keydown {args.seconds}s, tone {args.tone} Hz')
    print('  *** confirm dummy load connected, amp bypassed ***')
    print('=' * 60)

    radio = Radio(radio_ip='10.1.2.3', local_ip='10.1.2.185', variant='PRO',
                  verbose=True, auto_reconnect=False)
    if not radio.open():
        print('radio open failed'); sys.exit(1)
    radio.set_frequency(int(args.freq * 1000))
    radio.start_stream(lambda iq: None)
    time.sleep(1.0)
    if radio.telemetry:
        t = radio.telemetry
        print(f'pre-TX telemetry: {t["voltage"]:.1f}V {t["current"]:.2f}A '
              f'{t["temp_f"]:.0f}F')

    # No amp-protection watts limit (amp bypassed); bound by max_drive + dead-man.
    tx = TXSession(radio, mode='USB', realtime=True, max_drive=args.max_drive,
                   max_power_watts=None, deadman_s=args.seconds + 3, verbose=True)
    tx.arm(confirm=True)

    print(f'\n>>> KEYING for {args.seconds}s — READ THE WATTMETER <<<\n')
    tx.enter_tx(tone_iter(hz=args.tone, amp=args.amp), raw_drive=args.drive, pa=False)
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        time.sleep(1.0)
        if radio.telemetry:
            t = radio.telemetry
            print(f'  keyed t={time.time()-t0:.0f}s: {t["voltage"]:.1f}V '
                  f'{t["current"]:.2f}A {t["temp_f"]:.0f}F')
    tx.exit_tx()
    print('\n>>> UNKEYED <<<')
    st = tx.jitter()
    if st:
        print(f'TX pacing: {st["count"]} pkts, jitter max {st["max_dev_ms"]:.3f} ms')
    time.sleep(1.0)
    if radio.telemetry:
        t = radio.telemetry
        print(f'post-TX telemetry: {t["voltage"]:.1f}V {t["current"]:.2f}A '
              f'{t["temp_f"]:.0f}F')
    radio.close()


if __name__ == '__main__':
    main()
