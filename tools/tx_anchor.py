#!/usr/bin/env python3
"""
Wattmeter-anchor a single HF band: key full drive into the dummy load, hold for
a fixed window so the operator can read the through-line wattmeter, and capture
the SSA peak + DC telemetry for pairing with that reading.

PA-off throughout (pa=False, never sends 0x24). One band per invocation so the
operator paces the meter reads.

Usage:
  python3 tools/tx_anchor.py --freq 14074 --seconds 20
"""
import argparse
import sys
import time

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio
from solsdr.tx_session import TXSession
from rf_bench.siglent import SSA3000X

SA_IP = '10.1.1.60'
RADIO_IP = '10.1.2.3'
LOCAL_IP = '10.1.2.185'


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
    ap.add_argument('--freq', type=float, required=True, help='kHz')
    ap.add_argument('--seconds', type=float, default=20.0)
    ap.add_argument('--tone', type=float, default=1000.0)
    ap.add_argument('--amp', type=float, default=0.9)
    args = ap.parse_args()
    freq_hz = int(args.freq * 1000)
    carrier = freq_hz + args.tone

    radio = Radio(radio_ip=RADIO_IP, local_ip=LOCAL_IP, variant='PRO',
                  verbose=False, auto_reconnect=False)
    if not radio.open():
        print('radio open failed'); sys.exit(1)
    radio.set_frequency(freq_hz)
    radio.start_stream(lambda iq: None)
    time.sleep(0.8)
    idle = radio.telemetry or {}
    idle_dc = idle.get('voltage', 0) * idle.get('current', 0)

    tx = TXSession(radio, mode='USB', realtime=True, max_drive=255,
                   max_power_watts=None, deadman_s=args.seconds + 5, verbose=False)
    tx.arm(confirm=True)
    src = tone_audio(tone_hz=args.tone, amp=args.amp)

    with SSA3000X(SA_IP) as sa:
        print(f'\n>>> {args.freq:.0f} kHz FULL DRIVE for {args.seconds:.0f}s '
              f'— READ THE WATTMETER <<<\n')
        tx.enter_tx(src, raw_drive=255, pa=False)
        sa_readings = []
        dc_keyed = []  # capture DC WHILE keyed (not after unkey)
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            pf, pk = measure_sa(sa, carrier)
            sa_readings.append(pk)
            t = radio.telemetry or {}
            v = t.get('voltage', 0); a = t.get('current', 0)
            if v and a:
                dc_keyed.append(v * a)
            print(f'  t={time.time()-t0:4.0f}s  SA {pk:6.1f} dBm  '
                  f'{v:.1f}V {a:.2f}A {t.get("temp_f",0):.0f}F')
            time.sleep(1.0)
        tx.exit_tx()

    sa_med = float(np.median(sa_readings))
    dc = float(np.median(dc_keyed)) if dc_keyed else 0.0
    radio.close()
    print(f'\n=== {args.freq:.0f} kHz result ===')
    print(f'  median SA peak: {sa_med:.1f} dBm')
    print(f'  TX DC input   : {dc:.1f} W (idle {idle_dc:.1f} W, '
          f'delta {dc-idle_dc:.1f} W)')
    print(f'  --> tell me the WATTMETER reading for this band')
    print(f'ANCHOR freq_hz={freq_hz} sa_dbm={sa_med:.1f}')


if __name__ == '__main__':
    main()
