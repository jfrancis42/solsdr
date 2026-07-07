#!/usr/bin/env python3
"""
Per-band HF TX power calibration for the SunSDR2 PRO (PA-off, into dummy load).

For each HF band: key a steady full-drive USB carrier, read the SSA peak,
convert to watts via the wattmeter-anchored TapCal, and log the DC draw
(voltage/current) so PA efficiency can be sanity-checked per band (a bad tap
offset on some band shows up as absurd efficiency).

Also sweeps a few drive points per band to record the drive->watts curve, then
writes the result into the solsdr TXPowerCal table.

NEVER sends 0x24 (external-PA enable) — pa=False throughout.

Usage:
  python3 tools/tx_bandcal.py --dwell 2 --drives 64,128,192,255
  python3 tools/tx_bandcal.py --bands 20m,15m,10m --out /tmp/bandcal.json
"""
import argparse
import json
import sys
import time

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio
from solsdr.tx_session import TXSession
from solsdr.dsp.tap_cal import TapCal
from rf_bench.siglent import SSA3000X

SA_IP = '10.1.1.60'
RADIO_IP = '10.1.2.3'
LOCAL_IP = '10.1.2.185'

# One clear frequency per HF band (avoid band edges / known signals).
BANDS = {
    '160m': 1_900_000,
    '80m': 3_600_000,
    '60m': 5_357_000,
    '40m': 7_100_000,
    '30m': 10_120_000,
    '20m': 14_074_000,
    '17m': 18_100_000,
    '15m': 21_100_000,
    '12m': 24_920_000,
    '10m': 28_400_000,
}
BAND_ORDER = ['160m', '80m', '60m', '40m', '30m', '20m', '17m', '15m', '12m', '10m']


def tone_audio(tone_hz=1000.0, amp=0.9, audio_rate=48000):
    block = audio_rate // 50
    phase = 0.0
    dphi = 2 * np.pi * tone_hz / audio_rate
    while True:
        idx = np.arange(block)
        yield (amp * np.sin(phase + dphi * idx)).astype(np.float32)
        phase = float((phase + dphi * block) % (2 * np.pi))


def measure_sa(sa, freq_hz, span=6000, rbw=100):
    sa.setup_band(int(freq_hz - span / 2), int(freq_hz + span / 2))
    sa.write(f':SENS:BAND:RES {rbw}')
    sa.write(f':SENS:BAND:VID {rbw}')
    sa.single_sweep()
    return sa.get_peak()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bands', default=','.join(BAND_ORDER))
    ap.add_argument('--drives', default='64,128,192,255')
    ap.add_argument('--dwell', type=float, default=2.0)
    ap.add_argument('--tone', type=float, default=1000.0)
    ap.add_argument('--amp', type=float, default=0.9)
    ap.add_argument('--out', default='/tmp/solsdr_bandcal.json')
    args = ap.parse_args()
    bands = [b for b in args.bands.split(',') if b in BANDS]
    drives = [int(d) for d in args.drives.split(',')]

    cal = TapCal()
    if not cal.calibrated:
        print('ERROR: tap not calibrated'); sys.exit(1)
    print(f'TapCal offset {cal.offset_db:+.2f} dB (wattmeter-anchored)\n')

    radio = Radio(radio_ip=RADIO_IP, local_ip=LOCAL_IP, variant='PRO',
                  verbose=False, auto_reconnect=False)
    if not radio.open():
        print('radio open failed'); sys.exit(1)

    results = {}
    with SSA3000X(SA_IP) as sa:
        for band in bands:
            freq = BANDS[band]
            carrier = freq + args.tone
            radio.set_frequency(freq)
            radio.start_stream(lambda iq: None)
            time.sleep(0.8)
            idle = radio.telemetry or {}
            idle_dc = (idle.get('voltage', 0) * idle.get('current', 0))

            tx = TXSession(radio, mode='USB', realtime=True, max_drive=255,
                           max_power_watts=None,
                           deadman_s=args.dwell * len(drives) + 20, verbose=False)
            tx.arm(confirm=True)
            src = tone_audio(tone_hz=args.tone, amp=args.amp)

            print(f'=== {band} ({freq/1e6:.4f} MHz, loss '
                  f'{cal.path_loss_db(freq):.1f} dB) ===')
            print(f'{"drive":>5} {"SA dBm":>8} {"watts":>7} {"V":>6} {"A":>6} '
                  f'{"DCin":>6} {"eff%":>5}')
            curve = []
            for i, d in enumerate(drives):
                if i == 0:
                    tx.enter_tx(src, raw_drive=d, pa=False)
                else:
                    tx.set_drive_raw(d)
                time.sleep(args.dwell)
                peaks = [measure_sa(sa, carrier)[1] for _ in range(3)]
                sa_dbm = float(np.median(peaks))
                w = cal.sa_dbm_to_watts(sa_dbm, freq)
                t = radio.telemetry or {}
                v = t.get('voltage', 0); a = t.get('current', 0)
                dcin = v * a
                tx_dc = max(dcin - idle_dc, 0.001)
                eff = 100 * w / tx_dc if tx_dc > 0 else 0
                print(f'{d:>5} {sa_dbm:>8.1f} {w:>7.2f} {v:>6.1f} {a:>6.2f} '
                      f'{dcin:>6.1f} {eff:>5.0f}')
                curve.append({'drive': d, 'sa_dbm': sa_dbm, 'watts': round(w, 3),
                              'v': v, 'a': a, 'eff_pct': round(eff, 1)})
            tx.exit_tx()
            time.sleep(0.5)
            full = curve[-1]['watts']
            print(f'  -> {band} full-drive {full:.1f} W\n')
            results[band] = {'freq_hz': freq, 'idle_dc_w': round(idle_dc, 1),
                             'curve': curve, 'full_drive_w': full}

    radio.close()
    with open(args.out, 'w') as f:
        json.dump(results, f, indent=2)
    print(f'saved {args.out}')
    print('\n=== SUMMARY: full-drive power per band ===')
    for b in bands:
        r = results[b]
        print(f'  {b:>5}: {r["full_drive_w"]:5.1f} W  '
              f'(eff {r["curve"][-1]["eff_pct"]:.0f}%)')


if __name__ == '__main__':
    main()
