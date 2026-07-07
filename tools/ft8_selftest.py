#!/usr/bin/env python3
"""
FT8 self-test: objective proof the receiver works.

Records N seconds of demodulated USB audio from the SunSDR2 at 12 kHz (the rate
WSJT-X's jt9 decoder expects), aligned to the FT8 15-second cycle, writes a WAV,
and runs `jt9 -8` on it. Decoded callsigns = the receiver chain is correct.

This is the autonomous validation loop: no human needed to say "I hear signals."
If jt9 prints callsigns/grid squares, RX genuinely works.

Usage:
    python3 tools/ft8_selftest.py [freq_khz] [--cycles N]
"""
import argparse
import os
import subprocess
import sys
import time
import wave

import numpy as np

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio, PRO_WIRE_RATE
from solsdr.dsp.demod import Demodulator

FT8_AUDIO_RATE = 12000       # jt9 expects 12 kHz mono
FT8_PERIOD = 15              # FT8 cycle seconds
JT9 = '/usr/bin/jt9'
# jt9 (WSJT-X) may live on a different host than the radio. If JT9 isn't local,
# set DECODE_HOST to an SSH target that has jt9 and the WAV is scp'd there.
DECODE_HOST = os.environ.get('FT8_DECODE_HOST', '')  # e.g. 'greybox' / '10.1.0.16'


class FT8Recorder:
    def __init__(self, freq_khz):
        self.radio = Radio(radio_ip='10.1.2.3', local_ip='10.1.2.185',
                           variant='PRO', verbose=True)
        self.demod = Demodulator(wire_rate=PRO_WIRE_RATE,
                                 audio_rate=FT8_AUDIO_RATE, mode='USB', agc='off')
        # Disable AGC-style gain surges for cleaner decode: FT8 wants linear
        # audio. Use a fixed, modest gain instead of fast AGC.
        self.freq_hz = int(freq_khz * 1000)
        self.audio = []
        self.collecting = False

    def _on_iq(self, iq):
        if not self.collecting:
            return
        a = self.demod.process(iq)
        if len(a):
            self.audio.append(a)

    def open(self):
        if not self.radio.open():
            return False
        self.radio.start_stream(self._on_iq, freq_hz=self.freq_hz)
        time.sleep(1.0)  # let stream stabilize
        return True

    def record_cycle(self):
        """Record one 15-s FT8 window aligned to the UTC cycle boundary."""
        # Wait until the next 15-s boundary
        now = time.time()
        wait = FT8_PERIOD - (now % FT8_PERIOD)
        time.sleep(wait)
        self.audio = []
        self.collecting = True
        time.sleep(FT8_PERIOD + 0.5)
        self.collecting = False
        if not self.audio:
            return None
        return np.concatenate(self.audio)

    def close(self):
        self.radio.close()


def write_wav(path, audio, rate=FT8_AUDIO_RATE):
    # Normalize to int16
    peak = np.max(np.abs(audio)) or 1.0
    pcm = np.clip(audio / peak * 0.9, -1, 1)
    pcm16 = (pcm * 32767).astype(np.int16)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.tobytes())


def run_jt9(wav_path):
    """Run jt9 FT8 decoder locally or on DECODE_HOST, return decoded lines."""
    if DECODE_HOST:
        return _run_jt9_remote(wav_path, DECODE_HOST)
    if not os.path.exists(JT9):
        return [f'<jt9 not found at {JT9}; set FT8_DECODE_HOST to a host with jt9>']
    workdir = os.path.dirname(wav_path) or '.'
    try:
        p = subprocess.run(
            [JT9, '-8', '-a', workdir, '-t', workdir, wav_path],
            capture_output=True, text=True, timeout=60)
        out = (p.stdout + p.stderr).strip()
        return [l for l in out.splitlines() if l.strip()]
    except subprocess.TimeoutExpired:
        return ['<jt9 timeout>']


def _run_jt9_remote(wav_path, host):
    """scp the WAV to host and run jt9 there (for when the radio host has no
    WSJT-X). Returns decoded lines."""
    remote = f'/tmp/{os.path.basename(wav_path)}'
    try:
        subprocess.run(['scp', '-q', wav_path, f'{host}:{remote}'],
                       check=True, timeout=30)
        p = subprocess.run(
            ['ssh', host, f'cd /tmp && {JT9} -8 -a /tmp -t /tmp {remote}'],
            capture_output=True, text=True, timeout=60)
        out = (p.stdout + p.stderr).strip()
        return [l for l in out.splitlines() if l.strip()
                and not l.startswith('<DecodeFinished')]
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        return [f'<remote jt9 failed: {e}>']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('freq_khz', nargs='?', type=float, default=14074.0)
    ap.add_argument('--cycles', type=int, default=2)
    ap.add_argument('--outdir', default='/tmp/ft8test')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    rec = FT8Recorder(args.freq_khz)
    if not rec.open():
        print('radio open failed'); sys.exit(1)

    total_decodes = 0
    try:
        for c in range(args.cycles):
            print(f'\n=== cycle {c+1}/{args.cycles}: recording 15s @ {args.freq_khz} kHz ===')
            audio = rec.record_cycle()
            if audio is None:
                print('  no audio captured'); continue
            rms = np.sqrt(np.mean(audio ** 2))
            print(f'  captured {len(audio)} samples ({len(audio)/FT8_AUDIO_RATE:.1f}s) rms={rms:.4f}')
            wav = os.path.join(args.outdir, f'cycle{c+1}.wav')
            write_wav(wav, audio)
            decodes = run_jt9(wav)
            real = [d for d in decodes if not d.startswith('<')]
            print(f'  jt9 decoded {len(real)} message(s):')
            for d in real:
                print(f'    {d}')
            total_decodes += len(real)
    finally:
        rec.close()

    print(f'\n=== TOTAL FT8 DECODES: {total_decodes} ===')
    if total_decodes > 0:
        print('*** RECEIVER VALIDATED: real FT8 messages decoded from radio audio ***')
        sys.exit(0)
    else:
        print('No decodes — see diagnostics (may be band conditions or DSP tuning)')
        sys.exit(2)


if __name__ == '__main__':
    main()
