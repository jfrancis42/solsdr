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


class _RXChannel:
    """Per-receiver demod + filter + audio-out + IQ-sink state. AudioReceiver
    owns one (RX1) or two (RX1+RX2) of these; the RX-loop callback routes IQ to
    the right channel by receiver index."""
    def __init__(self, wire_rate, mode, chunk_n, device):
        self.demod = Demodulator(wire_rate=wire_rate, audio_rate=AUDIO_RATE,
                                 mode=mode)
        from solsdr.dsp.filters import RXFilterChain
        self.filters = RXFilterChain(AUDIO_RATE, cw_pitch=self.demod.cw_pitch)
        self._chunk_n = chunk_n
        self.device = device
        self.cw_decoder = None
        self.stream = None
        self.iq_sink = None            # raw-IQ fan-out (IQ server / recorder)
        self.iq_buf = np.zeros(0, dtype=np.complex64)
        self._lock = threading.Lock()
        self._print_cw = False         # print decoded CW to stdout (RX1 only)

    def feed(self, iq):
        if self.iq_sink is not None:
            try:
                self.iq_sink(iq)
            except Exception:
                pass
        with self._lock:
            self.iq_buf = np.concatenate([self.iq_buf, iq])
            while len(self.iq_buf) >= self._chunk_n:
                chunk = self.iq_buf[:self._chunk_n]
                self.iq_buf = self.iq_buf[self._chunk_n:]
                audio = self.demod.process(chunk)
                audio = self.filters.process(audio)
                if self.cw_decoder is not None and len(audio):
                    txt = self.cw_decoder.process(audio)
                    if txt and self._print_cw:
                        sys.stdout.write(txt); sys.stdout.flush()
                if self.stream and len(audio):
                    try:
                        self.stream.write(audio)
                    except Exception:
                        pass


class AudioReceiver:
    def __init__(self, freq_khz, mode='USB', device=5, local_ip='10.1.2.185',
                 radio_ip='10.1.2.3', variant='PRO', sample_rate=None,
                 ext_ref=None, rx2_khz=None, rx2_mode=None, rx2_device=None):
        self.freq_hz = int(freq_khz * 1000)
        self.device = device
        self.ext_ref = ext_ref
        self.rx2_hz = int(rx2_khz * 1000) if rx2_khz else None
        self.rx2_device = rx2_device
        self.radio = Radio(radio_ip=radio_ip, local_ip=local_ip,
                           variant=variant, auto_reconnect=True,
                           on_state_change=self._on_state_change,
                           sample_rate=sample_rate, rx2=self.rx2_hz is not None)
        # ~50 ms processing chunk, scaled to the wire rate (bigger at high rates
        # so the resampler stays well above realtime).
        self._chunk_n = max(2000, int(self.radio.wire_rate * 0.05) // 200 * 200)
        # Channel 0 = RX1 (always), channel 1 = RX2 (optional).
        self.channels = [_RXChannel(self.radio.wire_rate, mode, self._chunk_n,
                                    device)]
        self.channels[0]._print_cw = True
        if self.rx2_hz is not None:
            self.channels.append(_RXChannel(self.radio.wire_rate,
                                            rx2_mode or mode, self._chunk_n,
                                            rx2_device))

    def _on_state_change(self, state):
        # Surface reconnection activity to the user.
        print(f'\n[link] {state}')

    def _on_iq(self, iq):
        # Single-RX callback (1-arg): all IQ is RX1 -> channel 0.
        self.channels[0].feed(iq)

    def _on_iq_rx(self, rx_index, iq):
        # Two-receiver callback: route by receiver index.
        if 0 <= rx_index < len(self.channels):
            self.channels[rx_index].feed(iq)

    @property
    def demod(self):
        # Back-compat: RX1's demod (many callers reference rx.demod).
        return self.channels[0].demod

    @property
    def filters(self):
        # Back-compat: RX1's filter chain (shell nr/nb/notch/apf/sql commands).
        return self.channels[0].filters

    @property
    def iq_sink(self):
        return self.channels[0].iq_sink

    @iq_sink.setter
    def iq_sink(self, fn):
        # Back-compat: setting rx.iq_sink targets RX1's channel.
        self.channels[0].iq_sink = fn

    def _open_stream(self, device):
        s = sd.OutputStream(samplerate=AUDIO_RATE, channels=1, dtype='float32',
                            device=device, latency='high')
        s.start()
        return s

    def start(self):
        if not self.radio.open():
            print('failed to open radio')
            return False
        # Apply reference-clock preference if the user set --ext-ref / --no-ext-ref.
        if self.ext_ref is not None:
            self.radio.set_reference(self.ext_ref)
        # RX1 audio out.
        self.channels[0].stream = self._open_stream(self.device)
        if self.rx2_hz is not None:
            # RX2 audio out only if a device was given (else RX2 is IQ-only).
            if self.rx2_device is not None:
                self.channels[1].stream = self._open_stream(self.rx2_device)
            self.radio.start_stream(self._on_iq_rx, freq_hz=self.freq_hz)
            self.radio.set_frequency(self.rx2_hz, rx=1)
            self.radio.set_mode(self.channels[1].demod.mode, rx=1)
            print(f'RX1 {self.freq_hz/1000:.1f} kHz {self.channels[0].demod.mode} '
                  f'-> device {self.device}')
            print(f'RX2 {self.rx2_hz/1000:.1f} kHz {self.channels[1].demod.mode}'
                  + (f' -> device {self.rx2_device}' if self.rx2_device is not None
                     else ' (IQ only)'))
        else:
            self.radio.start_stream(self._on_iq, freq_hz=self.freq_hz)
            print(f'receiving {self.freq_hz/1000:.1f} kHz {self.demod.mode}, '
                  f'audio -> device {self.device}')
        return True

    def tune(self, freq_khz, rx=0):
        hz = int(freq_khz * 1000)
        if rx == 0:
            self.freq_hz = hz
        else:
            self.rx2_hz = hz
        self.radio.set_frequency(hz, rx=rx)

    def set_mode(self, mode, rx=0):
        ch = self.channels[rx]
        ch.demod.set_mode(mode)
        self.radio.set_mode(mode, rx=rx)
        ch.filters.apf.set(center_hz=ch.demod.cw_pitch)

    def cw_decode(self, on, rx=0):
        """Enable/disable the live Morse decoder (CW modes) for a receiver."""
        ch = self.channels[rx]
        if on:
            from solsdr.dsp.cw_decode import CWDecoder
            ch.cw_decoder = CWDecoder(sample_rate=AUDIO_RATE,
                                      pitch=ch.demod.cw_pitch)
            return True
        ch.cw_decoder = None
        return False

    def status(self):
        def one(label, ch, freq):
            return (f'{label} freq={ (freq or 0)/1000:.1f} kHz '
                    f'mode={ch.demod.mode} S={ch.demod.s_meter:.0f} dBFS')
        s = one('RX1', self.channels[0], self.radio.current_freq)
        if len(self.channels) > 1:
            s += '  ||  ' + one('RX2', self.channels[1], self.rx2_hz)
        s += (f' | pkts={self.radio.packets_received} '
              f'streaming={self.radio.streaming}')
        t = self.radio.telemetry
        if t:
            s += (f' | {t["voltage"]:.1f}V {t["current"]:.2f}A '
                  f'{t["temp_f"]:.0f}°F')
        return s

    def stop(self):
        self.radio.close()
        for ch in self.channels:
            if ch.stream:
                ch.stream.stop(); ch.stream.close()


def interactive_loop(rx):
    dual = len(rx.channels) > 1
    print('commands: <kHz> tune | m <mode> (USB/LSB/AM/FM/CW/CWU/CWL) | '
          'cw on|off (Morse decode) | ref ext|int (10 MHz reference) | '
          'nr <0-1> | nb <0-1> | notch <Hz|0> | apf <0-1> | sql <0-1> | '
          's status | q quit')
    if dual:
        print('  RX2 active: prefix tune/m/cw with "2 " for RX2 '
              '(e.g. "2 7074", "2 m CW"); bare commands act on RX1.')
    while True:
        try:
            line = input('sdr> ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ('q', 'quit', 'exit'):
            break
        # RX selector prefix: "1 <cmd>" / "2 <cmd>" targets RX1/RX2 for the
        # per-receiver commands (tune / m / cw). Bare commands default to RX1.
        target_rx = 0
        if len(line) > 2 and line[0] in '12' and line[1] == ' ':
            target_rx = int(line[0]) - 1
            line = line[2:].strip()
            if target_rx >= len(rx.channels):
                print(f'  RX{target_rx + 1} not active'); continue
        if line == 's':
            print('  ' + rx.status())
        elif line.startswith('m '):
            mode = line[2:].strip().upper()
            rx.set_mode(mode, rx=target_rx)
            print(f'  RX{target_rx + 1} mode -> {mode}')
        elif line.startswith('cw '):
            on = line[3:].strip().lower() in ('on', '1', 'true')
            rx.cw_decode(on, rx=target_rx)
            print(f'  RX{target_rx + 1} CW decode -> {"ON" if on else "off"}')
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
                rx.tune(khz, rx=target_rx)
                print(f'  RX{target_rx + 1} tuned -> {khz} kHz')
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
                    help='also run a real Hamlib rigctld (dummy backend) on :4532 '
                         'and mirror its freq/mode to the radio (requires rigctld '
                         'from Hamlib / libhamlib-utils)')
    ap.add_argument('--hamlib-port', type=int, default=4532,
                    help='port for the rigctld launched by --hamlib (default 4532)')
    ap.add_argument('--control-api', action='store_true',
                    help='also run the text control API on :5556')
    ap.add_argument('--iq-server', action='store_true',
                    help='also stream raw complex64 IQ to TCP clients on :5555 '
                         '(GNU Radio TCP source, recorders, etc.)')
    ap.add_argument('--iq-port', type=int, default=5555)
    ap.add_argument('--rx2', type=float, default=None, metavar='KHZ',
                    help='enable the second receiver at this frequency (kHz). '
                         'Both receivers share the one wire rate (--rate).')
    ap.add_argument('--rx2-mode', default=None,
                    help='demod mode for RX2 (default: same as --mode)')
    ap.add_argument('--rx2-device', type=int, default=None,
                    help='audio output device for RX2 (default: IQ/monitor only, '
                         'no audio). Use a second device for dual-watch listening.')
    ap.add_argument('--seconds', type=int, default=None,
                    help='run headless for N seconds then exit (no prompt)')
    args = ap.parse_args()

    rx = AudioReceiver(args.freq_khz, mode=args.mode, device=args.device,
                       local_ip=args.local_ip, radio_ip=args.radio_ip,
                       variant=args.variant, sample_rate=args.rate,
                       ext_ref=args.ext_ref, rx2_khz=args.rx2,
                       rx2_mode=args.rx2_mode, rx2_device=args.rx2_device)
    if not rx.start():
        sys.exit(1)

    servers = []
    if args.hamlib:
        # Launch a REAL rigctld (Hamlib dummy backend) and mirror its freq/mode
        # to the radio. RX-only here, so PTT is a no-op. This is the same
        # control model the JS8Call bridge uses — external software talks to
        # genuine Hamlib, not a hand-rolled protocol server.
        from solsdr.audio.rigctld_poller import RigctldPoller
        h = RigctldPoller(_RadioControlAdapter(rx), ptt_callback=lambda on: None,
                          port=args.hamlib_port)
        h.start(); servers.append(h)
        print(f'rigctld (real Hamlib, dummy backend) on :{args.hamlib_port}')
    if args.control_api:
        from solsdr.api.control_api import ControlAPIServer
        c = ControlAPIServer(_RadioControlAdapter(rx), port=5556)
        c.start(); servers.append(c)
        print('control API on :5556')
    if args.iq_server:
        from solsdr.api.iq_server import IQStreamServer
        iqs = IQStreamServer(port=args.iq_port)
        iqs.start(rate=rx.radio.wire_rate, freq=rx.freq_hz)
        rx.channels[0].iq_sink = iqs.publish
        servers.append(iqs)
        print(f'RX1 IQ stream server on :{args.iq_port} (complex64 @ '
              f'{rx.radio.wire_rate:.0f} Hz)')
        # RX2 on its own port (default 5557; 5556 is the control API).
        if args.rx2 is not None:
            rx2_port = args.iq_port + 2 if args.iq_port == 5555 else args.iq_port + 1
            iqs2 = IQStreamServer(port=rx2_port)
            iqs2.start(rate=rx.radio.wire_rate, freq=rx.rx2_hz)
            rx.channels[1].iq_sink = iqs2.publish
            servers.append(iqs2)
            print(f'RX2 IQ stream server on :{rx2_port} (complex64 @ '
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
