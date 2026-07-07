#!/usr/bin/env python3
"""
TX power calibration — interactive, at the wattmeter, into a DUMMY LOAD.

Keys the radio with a steady single tone at a chosen frequency, stepping through
a bounded sequence of drive bytes. At each step it shows the byte and its sqrt
estimate, you read the wattmeter and type the measured watts, and it records the
point into the per-band TXPowerCal table (JSON, persisted). After a few points
the band is "calibrated" and set_power(watts) becomes accurate for it.

SAFETY:
  * ALWAYS into a dummy load / wattmeter, never an antenna.
  * Starts at a very low drive byte and never exceeds --max-drive (default 64).
  * Constructs the TX session with NO watts limit (max_power_watts=None) because
    calibration is precisely the process that MAKES the watts limit trustworthy;
    it instead bounds itself by the raw --max-drive ceiling and small steps.
  * Requires an explicit typed confirmation before the first key-up.
  * Dead-man auto-unkey and per-step manual keying (keys only while measuring).

Usage (on the radio host):
  sudo python3 tools/tx_calibrate.py --freq 14074 --mode USB \\
       --drives 8,16,24,32,40,48,56,64
"""
import argparse
import sys
import time

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio
from solsdr.tx_session import TXSession
from solsdr.dsp.tx_power import TXPowerCal, band_for_freq, sqrt_model_drive


def tone_iter(audio_rate=48000, hz=1000, amplitude=0.5):
    """Endless steady single tone for tune/calibration."""
    block = audio_rate // 50
    phase = 0.0
    dphi = 2 * np.pi * hz / audio_rate
    while True:
        idx = np.arange(block)
        yield (amplitude * np.sin(phase + dphi * idx)).astype(np.float32)
        phase = (phase + dphi * block) % (2 * np.pi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--freq', type=float, required=True, help='TX frequency kHz')
    ap.add_argument('--mode', default='USB')
    ap.add_argument('--drives', default='8,16,24,32,40,48,56,64',
                    help='comma list of drive bytes to step through')
    ap.add_argument('--max-drive', type=int, default=64)
    ap.add_argument('--tone', type=float, default=1000, help='tone Hz')
    ap.add_argument('--dwell', type=float, default=6.0,
                    help='max seconds keyed per step (dead-man)')
    ap.add_argument('--pa', action='store_true', help='enable internal PA (0x24)')
    ap.add_argument('--calfile', default=None, help='override cal JSON path')
    ap.add_argument('--radio-ip', default='10.1.2.3')
    ap.add_argument('--local-ip', default='10.1.2.185')
    args = ap.parse_args()

    freq_hz = int(args.freq * 1000)
    band = band_for_freq(freq_hz)
    drives = [int(x) for x in args.drives.split(',') if x.strip()]
    drives = [d for d in drives if 0 < d <= args.max_drive]

    print('=' * 64)
    print('  SunSDR2 TX POWER CALIBRATION')
    print(f'  freq {args.freq} kHz  band {band}  mode {args.mode}')
    print(f'  drive bytes: {drives}  (max {args.max_drive})')
    print(f'  PA: {"ON" if args.pa else "off"}   dead-man {args.dwell}s/step')
    print('=' * 64)
    print('  *** CONNECT A DUMMY LOAD + WATTMETER. NOT AN ANTENNA. ***')
    resp = input('  Type "dummyload" to confirm the load is safe: ').strip()
    if resp != 'dummyload':
        print('aborted.'); sys.exit(1)

    cal = TXPowerCal(path=args.calfile) if args.calfile else TXPowerCal()

    radio = Radio(radio_ip=args.radio_ip, local_ip=args.local_ip, variant='PRO',
                  verbose=True, auto_reconnect=False)
    if not radio.open():
        print('radio open failed'); sys.exit(1)
    radio.set_frequency(freq_hz)
    # start RX streaming so the control link + keepalive are alive
    radio.start_stream(lambda iq: None)
    time.sleep(1.0)

    # No watts limit during calibration; bound by raw max_drive + small steps.
    tx = TXSession(radio, mode=args.mode, power_cal=cal, realtime=True,
                   max_drive=args.max_drive, max_power_watts=None,
                   deadman_s=args.dwell, verbose=True)
    tx.arm(confirm=True)

    try:
        for d in drives:
            est = None
            # inverse of sqrt model for a rough "expect ~X W" hint
            frac = d / 255.0
            est = frac * frac * cal.full_scale_w
            print(f'\n--- drive byte {d}  (sqrt estimate ~{est:.1f} W) ---')
            go = input(f'    press ENTER to KEY at byte {d} (or s=skip, q=quit): ').strip().lower()
            if go == 'q':
                break
            if go == 's':
                continue
            tx.enter_tx(tone_iter(hz=args.tone), raw_drive=d, pa=args.pa)
            print(f'    *** KEYED at byte {d} — read the wattmeter ***')
            watts_s = input('    measured watts (ENTER to re-key, blank skips record): ').strip()
            tx.exit_tx()
            print('    unkeyed.')
            if watts_s:
                try:
                    watts = float(watts_s)
                    cal.add_measurement(band, d, watts)
                    print(f'    recorded: {band} byte {d} = {watts} W  (saved)')
                except ValueError:
                    print('    not a number — skipped')
            time.sleep(0.5)  # let RX resume between steps
    finally:
        tx.exit_tx()
        radio.close()

    print('\n=== calibration points for', band, '===')
    for d, w in cal.points.get(band, []):
        print(f'  byte {d:3d} -> {w} W')
    if cal.is_calibrated(band):
        print(f'{band} is now CALIBRATED — set_power(watts) is accurate here.')
    else:
        print(f'{band} needs >=2 points to be usable; add more.')


if __name__ == '__main__':
    main()
