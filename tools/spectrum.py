#!/usr/bin/env python3
"""
Render a spectrum + waterfall PNG from captured IQ (.npy) or live from the
radio. Visual verification of what the receiver sees.

Usage:
    # from a saved IQ capture
    python3 tools/spectrum.py --npy /tmp/iqcap_local/iq_cycle1.npy --out /tmp/spec.png
    # live (run on the radio host)
    python3 tools/spectrum.py --freq 14074 --seconds 5 --out /tmp/spec.png
"""
import argparse
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')


def render(iq, wire_rate, out_path, title=''):
    # Spectrum (averaged PSD)
    nfft = 4096
    nrows = max(1, len(iq) // nfft)
    spec = np.zeros((nrows, nfft))
    win = np.hanning(nfft)
    for r in range(nrows):
        seg = iq[r * nfft:(r + 1) * nfft]
        if len(seg) < nfft:
            break
        f = np.fft.fftshift(np.fft.fft(seg * win))
        spec[r] = 20 * np.log10(np.abs(f) + 1e-9)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / wire_rate)) / 1000  # kHz offset
    psd = spec.mean(axis=0)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8),
                                   gridspec_kw={'height_ratios': [1, 2]})
    ax1.plot(freqs, psd, lw=0.6)
    ax1.set_ylabel('dB'); ax1.set_xlabel('kHz from tuned freq')
    ax1.set_title(title + '  (averaged spectrum)')
    ax1.grid(True, alpha=0.3)

    vmax = np.percentile(spec, 99)
    vmin = np.percentile(spec, 5)
    ax2.imshow(spec, aspect='auto', origin='lower', cmap='viridis',
               vmin=vmin, vmax=vmax,
               extent=[freqs[0], freqs[-1], 0, nrows])
    ax2.set_ylabel('time (frames)'); ax2.set_xlabel('kHz from tuned freq')
    ax2.set_title('waterfall')
    plt.tight_layout()
    plt.savefig(out_path, dpi=90)
    print(f'wrote {out_path}  ({nrows} frames, {len(iq)} samples @ {wire_rate:.0f} Hz)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--npy', help='saved complex IQ capture')
    ap.add_argument('--wire-rate', type=float, default=39062.5)
    ap.add_argument('--freq', type=float, help='live: tune kHz')
    ap.add_argument('--seconds', type=float, default=5)
    ap.add_argument('--out', default='/tmp/spectrum.png')
    args = ap.parse_args()

    if args.npy:
        iq = np.load(args.npy)
        render(iq, args.wire_rate, args.out, title=f'{args.npy}')
    elif args.freq:
        import time
        from solsdr.radio import Radio
        r = Radio(radio_ip='10.1.2.3', local_ip='10.1.2.185', variant='PRO',
                  verbose=False)
        buf = {'iq': [], 'on': False}
        r.open()
        r.start_stream(lambda iq: buf['iq'].append(iq) if buf['on'] else None,
                       freq_hz=int(args.freq * 1000))
        time.sleep(1); buf['on'] = True; time.sleep(args.seconds); buf['on'] = False
        r.close()
        iq = np.concatenate(buf['iq'])
        render(iq, r.wire_rate, args.out, title=f'{args.freq} kHz')
    else:
        print('need --npy or --freq'); sys.exit(1)


if __name__ == '__main__':
    main()
