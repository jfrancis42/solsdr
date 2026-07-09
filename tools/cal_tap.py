#!/usr/bin/env python3
"""
Calibrate the tap+attenuator path loss using the SSA3032X tracking generator.

Ratiometric (normalized) two-step measurement — accuracy comes from the
TG->SA path being measured against itself, so the SSA's absolute-amplitude
spec doesn't dominate:

  Step 1 (reference): TG output -> SA input DIRECTLY (short cable, tap bypassed).
                      Measured level = P_ref.
  Step 2 (through):   TG output -> the tap where the RADIO's antenna port
                      normally connects -> tap's SA-sample output -> SA input.
                      Measured level = P_tap.

  Path loss  L = P_ref - P_tap   (dB, positive)

Saved to ~/.config/solsdr/tap_cal.json. Then real TX power is:
      P_radio(dBm) = SA_peak(dBm) + L
      watts = 10**((P_radio - 30)/10)

Measured at several frequencies across the HF range so we can interpolate
(attenuator/coupler loss is fairly flat, but tap coupling can vary with freq).

Usage: python3 tools/cal_tap.py            (interactive; prompts to move cable)
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from rf_bench.siglent import SSA3000X

SA_IP = '10.1.1.60'
CAL_PATH = os.path.expanduser('~/.config/solsdr/tap_cal.json')
# frequencies to characterize the path at (Hz)
CAL_FREQS = [1_900_000, 3_600_000, 7_100_000, 14_100_000,
             21_100_000, 28_500_000, 50_100_000]
TG_LEVEL = 0.0   # dBm, max TG output for best SNR


def measure_at(sa, freq_hz, rbw=100, span=20000):
    """TG is CW at the SA center; read the peak level at freq_hz."""
    sa.setup_band(int(freq_hz - span / 2), int(freq_hz + span / 2))
    sa.write(f':SENS:BAND:RES {rbw}')
    sa.write(f':SENS:BAND:VID {rbw}')
    sa.single_sweep()
    pf, pk = sa.get_peak()
    return pk


def sweep(sa, label):
    print(f'  measuring {label} at {len(CAL_FREQS)} frequencies...')
    out = {}
    for f in CAL_FREQS:
        lvl = measure_at(sa, f)
        out[f] = lvl
        print(f'    {f/1e6:6.1f} MHz: {lvl:6.1f} dBm')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=CAL_PATH)
    args = ap.parse_args()

    sa = SSA3000X(SA_IP)
    sa.connect()
    print('Enabling tracking generator at', TG_LEVEL, 'dBm')
    sa.enable_tracking_generator(TG_LEVEL)
    time.sleep(0.5)

    try:
        print('\n=== STEP 1: REFERENCE ===')
        print('Connect the SSA tracking-generator OUTPUT directly to the SSA INPUT')
        print('(short cable, tap/attenuator BYPASSED).')
        input('Press ENTER when connected... ')
        ref = sweep(sa, 'reference (TG->SA direct)')

        print('\n=== STEP 2: THROUGH THE TAP ===')
        print('Now route: TG OUTPUT -> the tap where the RADIO antenna port')
        print('normally connects -> tap SA-sample output -> SSA INPUT.')
        print('(i.e. put the tap+attenuator in the path exactly as during TX)')
        input('Press ENTER when connected... ')
        thru = sweep(sa, 'through tap')

        loss = {f: round(ref[f] - thru[f], 2) for f in CAL_FREQS}
        print('\n=== PATH LOSS (dB) ===')
        for f in CAL_FREQS:
            print(f'  {f/1e6:6.1f} MHz: {loss[f]:6.2f} dB')

        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, 'w') as fp:
            json.dump({'tg_level_dbm': TG_LEVEL,
                       'reference_dbm': ref, 'through_dbm': thru,
                       'loss_db': loss}, fp, indent=2)
        print(f'\nsaved to {args.out}')
        print('Real TX power: P_radio(dBm) = SA_peak + loss_at_freq; '
              'watts = 10**((P_radio-30)/10)')
    finally:
        sa.disable_tracking_generator()
        sa.close()


if __name__ == '__main__':
    main()
