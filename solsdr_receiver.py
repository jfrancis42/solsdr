#!/usr/bin/env python3
"""
SunSDR2 PRO receiver — consolidated, verified implementation.

Wakes/discovers the radio, powers it on, tunes, streams IQ, demodulates, and
plays audio. Supports live retune/mode changes at the prompt (no restart) and
optional Hamlib rigctld + text control API servers so external software can
control it.

Usage:
    python3 solsdr_receiver.py [freq_khz] [--mode USB] [--device 5]
                                [--hamlib] [--control-api]

Interactive commands (type at the "sdr>" prompt while running):
    <number>        tune to that many kHz     (e.g. 14074)
    m <mode>        set mode USB/LSB/AM/FM/CW
    s               show S-meter + status
    q               quit
"""
import argparse
import sys
import threading
import time

import numpy as np
import sounddevice as sd

sys.path.insert(0, '/home/jfrancis/Dropbox/build/solsdr')
from solsdr.radio import Radio, PRO_WIRE_RATE
from solsdr.dsp.demod import Demodulator

AUDIO_RATE = 48000


class _RadioControlAdapter:
    """Adapts AudioReceiver to the control-object interface the API servers
    expect (set_frequency/set_mode/current_freq/current_mode/streaming)."""
    def __init__(self, rx):
        self._rx = rx

    def set_frequency(self, hz):
        self._rx.tune(hz / 1000.0); return True

    def set_mode(self, mode):
        self._rx.set_mode(mode); return True

    def set_ptt(self, on):
        return False  # RX-only for now

    @property
    def current_freq(self):
        return self._rx.radio.current_freq

    @property
    def current_mode(self):
        return self._rx.demod.mode

    @property
    def streaming(self):
        return self._rx.radio.streaming


class AudioReceiver:
    def __init__(self, freq_khz, mode='USB', device=5, local_ip='10.1.2.185',
                 radio_ip='10.1.2.3', variant='PRO', sample_rate=None,
                 ext_ref=None):
        self.freq_hz = int(freq_khz * 1000)
        self.device = device
        self.ext_ref = ext_ref
        self.radio = Radio(radio_ip=radio_ip, local_ip=local_ip,
                           variant=variant, auto_reconnect=True,
                           on_state_change=self._on_state_change,
                           sample_rate=sample_rate)
        # Demod runs at the radio's actual IQ rate (verified PRO: 39062.5,
        # 78125, 156250, or 312500 Hz depending on --rate).
        self.demod = Demodulator(wire_rate=self.radio.wire_rate,
                                 audio_rate=AUDIO_RATE, mode=mode)
        # ~50 ms processing chunk, scaled to the wire rate (bigger at high rates
        # so the resampler stays well above realtime).
        self._chunk_n = max(2000, int(self.radio.wire_rate * 0.05) // 200 * 200)
        # Post-demod stateful filter chain (NR/NB/notch/APF/squelch), all off
        # by default — enable via the interactive shell.
        from solsdr.dsp.filters import RXFilterChain
        self.filters = RXFilterChain(AUDIO_RATE, cw_pitch=self.demod.cw_pitch)
        # Optional live Morse decoder (created on demand in CW mode).
        self.cw_decoder = None
        self.stream = None
        self.iq_buf = np.zeros(0, dtype=np.complex64)
        self._lock = threading.Lock()
        # Optional raw-IQ fan-out (e.g. IQStreamServer.publish) for network
        # clients like GNU Radio. Receives every raw IQ block before demod.
        self.iq_sink = None

    def _on_state_change(self, state):
        # Surface reconnection activity to the user.
        print(f'\n[link] {state}')

    def _on_iq(self, iq):
        # Fan raw IQ out to any network sink first (GNU Radio clients, etc.).
        if self.iq_sink is not None:
            try:
                self.iq_sink(iq)
            except Exception:
                pass
        # Accumulate and process in ~50 ms chunks. Scale the chunk with the wire
        # rate (2000 @ 39 kHz, ~16k @ 312.5 kHz) so the resampler's per-call FIR
        # setup amortizes and high rates stay comfortably above realtime.
        chunk_n = self._chunk_n
        with self._lock:
            self.iq_buf = np.concatenate([self.iq_buf, iq])
            while len(self.iq_buf) >= chunk_n:
                chunk = self.iq_buf[:chunk_n]
                self.iq_buf = self.iq_buf[chunk_n:]
                audio = self.demod.process(chunk)
                audio = self.filters.process(audio)
                # Live CW decode: feed the decoder and print decoded text.
                if self.cw_decoder is not None and len(audio):
                    txt = self.cw_decoder.process(audio)
                    if txt:
                        sys.stdout.write(txt); sys.stdout.flush()
                if self.stream and len(audio):
                    try:
                        self.stream.write(audio)
                    except Exception:
                        pass

    def start(self):
        if not self.radio.open():
            print('failed to open radio')
            return False
        # Apply reference-clock preference if the user set --ext-ref / --no-ext-ref.
        if self.ext_ref is not None:
            self.radio.set_reference(self.ext_ref)
        self.stream = sd.OutputStream(samplerate=AUDIO_RATE, channels=1,
                                      dtype='float32', device=self.device,
                                      latency='high')
        self.stream.start()
        self.radio.start_stream(self._on_iq, freq_hz=self.freq_hz)
        print(f'receiving {self.freq_hz/1000:.1f} kHz {self.demod.mode}, '
              f'audio -> device {self.device}')
        return True

    def tune(self, freq_khz):
        self.freq_hz = int(freq_khz * 1000)
        self.radio.set_frequency(self.freq_hz)

    def set_mode(self, mode):
        self.demod.set_mode(mode)
        self.radio.set_mode(mode)
        # keep the APF center on the CW pitch when relevant
        self.filters.apf.set(center_hz=self.demod.cw_pitch)

    def cw_decode(self, on):
        """Enable/disable the live Morse decoder (CW modes)."""
        if on:
            from solsdr.dsp.cw_decode import CWDecoder
            self.cw_decoder = CWDecoder(sample_rate=AUDIO_RATE,
                                        pitch=self.demod.cw_pitch)
            return True
        self.cw_decoder = None
        return False

    def status(self):
        f = self.radio.current_freq or 0
        s = (f'freq={f/1000:.1f} kHz mode={self.demod.mode} '
             f'S={self.demod.s_meter:.0f} dBFS '
             f'pkts={self.radio.packets_received} '
             f'streaming={self.radio.streaming}')
        t = self.radio.telemetry
        if t:
            s += (f' | {t["voltage"]:.1f}V {t["current"]:.2f}A '
                  f'{t["temp_f"]:.0f}°F')
        return s

    def stop(self):
        self.radio.close()
        if self.stream:
            self.stream.stop(); self.stream.close()


def interactive_loop(rx):
    print('commands: <kHz> tune | m <mode> (USB/LSB/AM/FM/CW/CWU/CWL) | '
          'cw on|off (Morse decode) | ref ext|int (10 MHz reference) | '
          'nr <0-1> | nb <0-1> | notch <Hz|0> | apf <0-1> | sql <0-1> | '
          's status | q quit')
    while True:
        try:
            line = input('sdr> ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ('q', 'quit', 'exit'):
            break
        if line == 's':
            print('  ' + rx.status())
        elif line.startswith('m '):
            mode = line[2:].strip().upper()
            rx.set_mode(mode)
            print(f'  mode -> {mode}')
        elif line.startswith('cw '):
            on = line[3:].strip().lower() in ('on', '1', 'true')
            rx.cw_decode(on)
            print(f'  CW decode -> {"ON" if on else "off"}')
        elif line.startswith('ref '):
            ext = line[4:].strip().lower() in ('ext', 'external', 'on', '1')
            rx.radio.set_reference(ext)
            print(f'  reference -> {"external 10 MHz (GPSDO)" if ext else "internal"}')
        elif line.startswith('lpf '):
            on = line[4:].strip().lower() in ('on', '1', 'hf', 'true')
            rx.radio.set_hf_lpf(on)
            print(f'  HF.LPF -> {"engaged" if on else "auto"}')
        elif line.startswith('lna '):
            on = line[4:].strip().lower() in ('on', '1', 'true')
            rx.radio.set_vhf_lna(on)
            print(f'  VHF.LNA -> {"on" if on else "off"}')
        elif line.startswith('mic '):
            src = line[4:].strip().lower()
            try:
                rx.radio.set_mic_source(src)
                print(f'  mic source -> {src}')
            except ValueError as e:
                print(f'  {e}')
        elif line.startswith('nr '):
            rx.filters.nr.level = float(line[3:]); print(f'  NR={rx.filters.nr.level}')
        elif line.startswith('nb '):
            rx.filters.nb.level = float(line[3:]); print(f'  NB={rx.filters.nb.level}')
        elif line.startswith('notch '):
            rx.filters.notch.set_notch(float(line[6:])); print(f'  notch={line[6:].strip()} Hz')
        elif line.startswith('apf '):
            rx.filters.apf.set(level=float(line[4:])); print(f'  APF={line[4:].strip()}')
        elif line.startswith('sql '):
            rx.filters.squelch.level = float(line[4:]); print(f'  squelch={rx.filters.squelch.level}')
        else:
            try:
                khz = float(line)
                rx.tune(khz)
                print(f'  tuned -> {khz} kHz')
            except ValueError:
                print('  ? see command list above')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('freq_khz', nargs='?', type=float, default=14074.0)
    ap.add_argument('--mode', default='USB')
    ap.add_argument('--device', type=int, default=5,
                    help='audio device (5 = pipewire; 3 = raw ALSA hw)')
    ap.add_argument('--local-ip', default='10.1.2.185')
    ap.add_argument('--radio-ip', default='10.1.2.3')
    ap.add_argument('--variant', default='PRO', choices=['PRO', 'DX'],
                    help='radio model. PRO is hardware-verified; DX is from the '
                         'ArtemisSDR reference and UNVERIFIED (may not work).')
    ap.add_argument('--rate', type=float, default=None,
                    choices=[39062.5, 78125.0, 156250.0, 312500.0],
                    help='PRO IQ sample rate in Hz (default 39062.5). Higher '
                         'rates give wider spectrum but 2-8x the CPU/network.')
    ref = ap.add_mutually_exclusive_group()
    ref.add_argument('--ext-ref', dest='ext_ref', action='store_true',
                     default=None,
                     help='use the external 10 MHz reference (GPSDO)')
    ref.add_argument('--no-ext-ref', dest='ext_ref', action='store_false',
                     help='use the internal reference (default leaves it as-is; '
                          'the PRO boots with external reference enabled)')
    ap.add_argument('--hamlib', action='store_true',
                    help='also run a Hamlib rigctld server on :4532')
    ap.add_argument('--control-api', action='store_true',
                    help='also run the text control API on :5556')
    ap.add_argument('--iq-server', action='store_true',
                    help='also stream raw complex64 IQ to TCP clients on :5555 '
                         '(GNU Radio TCP source, recorders, etc.)')
    ap.add_argument('--iq-port', type=int, default=5555)
    ap.add_argument('--seconds', type=int, default=None,
                    help='run headless for N seconds then exit (no prompt)')
    args = ap.parse_args()

    rx = AudioReceiver(args.freq_khz, mode=args.mode, device=args.device,
                       local_ip=args.local_ip, radio_ip=args.radio_ip,
                       variant=args.variant, sample_rate=args.rate,
                       ext_ref=args.ext_ref)
    if not rx.start():
        sys.exit(1)

    servers = []
    if args.hamlib:
        from solsdr.api.hamlib_compat import HamlibServer
        h = HamlibServer(_RadioControlAdapter(rx), port=4532)
        h.start(); servers.append(h)
        print('hamlib rigctld server on :4532')
    if args.control_api:
        from solsdr.api.control_api import ControlAPIServer
        c = ControlAPIServer(_RadioControlAdapter(rx), port=5556)
        c.start(); servers.append(c)
        print('control API on :5556')
    if args.iq_server:
        from solsdr.api.iq_server import IQStreamServer
        iqs = IQStreamServer(port=args.iq_port)
        iqs.start(rate=rx.radio.wire_rate, freq=rx.freq_hz)
        rx.iq_sink = iqs.publish
        servers.append(iqs)
        print(f'IQ stream server on :{args.iq_port} (complex64 @ '
              f'{rx.radio.wire_rate:.0f} Hz)')

    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            interactive_loop(rx)
    finally:
        for s in servers:
            s.stop()
        rx.stop()


if __name__ == '__main__':
    main()
