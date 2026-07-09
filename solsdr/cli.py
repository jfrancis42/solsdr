#!/usr/bin/env python3
"""
solsdr — SunSDR2 PRO transceiver shell (the `solsdr` command / `python3 -m solsdr`).

One program for RX and TX: wakes/discovers the radio, powers it on, tunes,
streams IQ, demodulates, plays audio, AND brings the digital-mode/TX bridge up
in-process (virtual audio + Hamlib rigctld + PTT). The interactive shell controls
the whole radio — receive, DSP, front-end, and transmit — with a `tune` and
`cw <text>` that key the transmitter directly. Live retune/mode changes at the
prompt, no restart. Optional text control API + raw-IQ servers for other tools.

Usage:
    python3 -m solsdr [freq_khz] [--mode USB] [--device 5] [--no-tx] ...
    solsdr 14074        # once installed (pip install .)

Type `help` at the "sdr>" prompt for the full command list.
"""
import argparse
import sys
import threading
import time

import numpy as np
import sounddevice as sd

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

    # RX DSP + front-end controls exposed to the network control API (act on
    # RX1's channel; the radio-level preamp affects the whole front end).
    def set_preamp(self, state):
        return self._rx.radio.set_preamp(state)

    def set_rit(self, hz):
        self._rx.channels[0].demod.set_rit(hz); return True

    def set_squelch(self, level):
        self._rx.channels[0].filters.squelch.level = float(level); return True

    def set_agc(self, mode):
        self._rx.channels[0].demod.set_agc(mode); return True

    def set_nr(self, level):
        self._rx.channels[0].filters.nr.level = float(level); return True

    @property
    def current_freq(self):
        return self._rx.radio.current_freq

    @property
    def current_mode(self):
        return self._rx.demod.mode

    @property
    def streaming(self):
        return self._rx.radio.streaming

    @property
    def s_meter(self):
        # RX1's smoothed signal level in dBFS (already computed each block).
        return self._rx.channels[0].demod.s_meter


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
        self.bridge = None               # in-process TX/digital-mode bridge (opt)
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
        if self.bridge is not None:      # fan RX1 IQ out to the in-process bridge
            self.bridge.feed_iq(iq)

    def _on_iq_rx(self, rx_index, iq):
        # Two-receiver callback: route by receiver index.
        if 0 <= rx_index < len(self.channels):
            self.channels[rx_index].feed(iq)
        if rx_index == 0 and self.bridge is not None:
            self.bridge.feed_iq(iq)      # bridge follows RX1

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
                    f'mode={ch.demod.mode} S={ch.demod.s_meter:.0f} dBFS '
                    f'agc={ch.demod.agc_mode}')
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


def _print_help(dual):
    print("""
solsdr — interactive transceiver shell.  One program for RX and TX: the digital-
mode/TX bridge runs in-process (virtual audio + rigctld + PTT), so this shell
controls the whole radio.  `tx ...` sets transmit characteristics live; `tune`
and `cw <text>` key the transmitter directly.  ⚠ Transmit needs an antenna or
dummy load, and obeys the amp-limit / calibration interlocks.

  TUNING / MODE
    <kHz>              tune to frequency in kHz            (e.g. 7074)
    m <mode>           mode: USB LSB AM FM CW CWU CWL
    cw on|off          live Morse (CW) decode (RX)
    cw pitch <Hz>      CW RX beat-note pitch (e.g. cw pitch 700)
    cw bw <Hz>         CW RX filter bandwidth (e.g. cw bw 200)
    cw <text>          ⚠ TRANSMIT text as Morse  (e.g. cw cq cq de n0gq)
    rit <Hz>           receiver incremental tuning (0 = off)

  RX AUDIO OUTPUT LEVEL
    agc <mode>         auto | on | off | fixed:<gain>      (e.g. agc fixed:5000)
    gain <n>           set fixed audio gain (implies agc off; e.g. gain 8000)
    vol <n>            alias for gain

  TRANSMIT SETTINGS   (saved to config; the TX path applies them live)
    tx                 show all current TX settings
    tx power <W>       TX output setpoint in watts        (e.g. tx power 3)
    tx maxpower <W>    amp-protection ceiling in watts (safety; None = off)
    tx mode <mode>     TX modulation mode (USB/LSB/AM/FM)
    tx micgain <x>     mic/TX-audio gain multiplier (1.0 = unity; e.g. 1.5)
    tx wpm <c> [<w>]   CW send speed: element <c> wpm, optional Farnsworth
                       spacing <w> wpm (w<c). e.g. "tx wpm 25 15" = 15wpm@25
    tx cwtone <Hz>     CW send sidetone/pitch (default 600)
    tx prefix <name>   rename the virtual audio devices live -> <name>-rx.monitor
                       (fldigi/WSJT-X input) + <name>-tx (output). Drops apps
                       bound to the old names. (Set at launch with --prefix.)
    tune [s] [W]       ⚠ KEY a CW tuning carrier: s sec (default 3),
                       W watts (default current power). e.g. "tune", "tune 5 3"

  FRONT END / RADIO
    preamp <state>     -20 | -10 | 0 | +10 (dB) | off | preamp
    ref ext|int        10 MHz reference (external GPSDO / internal)
    lpf on|off         HF low-pass filter
    lna on|off         VHF LNA
    mic <src>          mic SOURCE at the radio (mic1 | mic2 | pc)

  DSP FILTERS
    nr <0-1>           noise reduction        nb <0-1>   noise blanker
    notch <Hz|0>       manual notch (0=off)   apf <0-1>  audio peak filter (CW)
    sql <0-1>          squelch threshold

  INFO / CONTROL
    s | status         show freq/mode/level/status
    devices            list available audio devices
    help | ?           this help
    q | quit           quit
""".rstrip())
    if dual:
        print('  RX2 active: prefix per-receiver commands with "1 "/"2 " '
              '(e.g. "2 7074", "2 m CW"); bare commands act on RX1.')


def _tx_command(rx, arg):
    """Handle `tx` / `tx <setting> <value>` against the LIVE in-process bridge.
    Changes take effect immediately — including on an in-progress transmission
    (power/mic gain). max-power is a safety ceiling and only applies to the next
    over (never raised live). Settings are session-live only (not persisted)."""
    br = getattr(rx, 'bridge', None)
    if br is None:
        print('  TX not available (no bridge — PulseAudio/rigctld missing?). '
              'See startup messages.'); return
    parts = arg.split(None, 1)
    if not parts:                      # bare `tx` -> show all
        keyed = br.is_keyed()
        print(f'  TX settings (live{"; KEYED now" if keyed else ""}):')
        print(f'    power    = {br.tx_watts if br.tx_watts is not None else "full drive"} W'
              f'   (setpoint; applies live while keyed)')
        print(f'    maxpower = {br.max_power_watts if br.max_power_watts is not None else "none"} W'
              f'   (amp-protection ceiling; next over only)')
        print(f'    mode     = {br.tx_mode}   (next over)')
        print(f'    micgain  = {br.mic_gain:g}   (TX audio gain; applies live)')
        fw = (f' Farnsworth (elements {br.cw_char_wpm:g}wpm)'
              if br.cw_word_wpm and br.cw_word_wpm < br.cw_char_wpm else '')
        eff = br.cw_word_wpm or br.cw_char_wpm
        print(f'    wpm      = {br.cw_char_wpm:g}'
              f'{" / %g" % br.cw_word_wpm if br.cw_word_wpm else ""}'
              f'   (CW send: char/word; effective {eff:g}wpm{fw})')
        print(f'    cwtone   = {br.cw_tone_hz:g} Hz   (CW send sidetone/pitch)')
        d = br.devices
        print(f'    prefix   = {d.prefix!r}')
        print(f'      -> fldigi/WSJT-X INPUT  (RX audio): {d.rx_sink_name}.monitor'
              f'  (or {d.rx_input_name})')
        print(f'      -> fldigi/WSJT-X OUTPUT (TX audio): {d.tx_sink_name}')
        print(f'    monitor  = {br.monitor.sink or "off"}')
        return
    word = parts[0].lower()
    val = parts[1].strip() if len(parts) > 1 else None
    try:
        if word == 'power':
            if val is None:
                print(f'  power = {br.tx_watts} W'); return
            w = None if val.lower() in ('none', 'off', 'full') else float(val)
            br.set_tx_watts(w)
            print(f'  tx power -> {w if w is not None else "full drive"} W'
                  f'{" (applied to live TX)" if br.is_keyed() else ""}')
        elif word == 'maxpower':
            if val is None:
                print(f'  maxpower = {br.max_power_watts} W'); return
            w = None if val.lower() in ('none', 'off') else float(val)
            br.max_power_watts = w
            print(f'  tx maxpower -> {w} W (amp ceiling; applies on the NEXT over — '
                  f'never raised on a live transmission, by design)')
        elif word == 'mode':
            if val is None:
                print(f'  mode = {br.tx_mode}'); return
            br.tx_mode = val.upper()
            print(f'  tx mode -> {br.tx_mode} (next over)')
        elif word == 'micgain':
            if val is None:
                print(f'  micgain = {br.mic_gain:g}'); return
            br.set_mic_gain(float(val))
            print(f'  tx micgain -> {br.mic_gain:g}'
                  f'{" (applied to live TX)" if br.is_keyed() else ""}')
        elif word == 'wpm':
            if val is None:
                print(f'  wpm = {br.cw_char_wpm:g}'
                      f'{" / %g (Farnsworth)" % br.cw_word_wpm if br.cw_word_wpm else ""}')
                return
            # "tx wpm <char> [<word>]" — word<char = Farnsworth (slower spacing)
            nums = val.split()
            char_wpm = float(nums[0])
            word_wpm = float(nums[1]) if len(nums) > 1 else None
            br.set_cw(char_wpm=char_wpm, word_wpm=word_wpm)
            if word_wpm and word_wpm < char_wpm:
                print(f'  tx wpm -> {char_wpm:g} wpm elements, {word_wpm:g} wpm '
                      f'effective (Farnsworth)')
            else:
                print(f'  tx wpm -> {char_wpm:g} wpm (standard timing)')
        elif word == 'cwtone':
            if val is None:
                print(f'  cwtone = {br.cw_tone_hz:g} Hz'); return
            br.set_cw(tone_hz=float(val))
            print(f'  tx cwtone -> {br.cw_tone_hz:g} Hz')
        elif word == 'prefix':
            if val is None:
                print(f'  prefix = {br.devices.prefix!r}'); return
            print(f'  renaming virtual audio devices to {val!r} — '
                  f'apps bound to the old names will drop, repoint them.')
            ok, msg = br.set_prefix(val)
            print(f'  {msg}')
        else:
            print(f'  ? unknown tx setting {word!r}; try: '
                  f'power maxpower mode micgain wpm cwtone prefix '
                  f'(bare "tx" shows all)')
    except ValueError:
        print(f'  bad value for tx {word}: {val!r}')


def _tune_command(rx, arg):
    """`tune [seconds] [watts]` — key a CW tuning carrier, then unkey.

    The one shell command that transmits. Positional args: duration in seconds
    (default 3) and power in watts (default: current TX setpoint). Blocks for the
    duration. ⚠️ transmits — antenna/dummy load required."""
    br = getattr(rx, 'bridge', None)
    if br is None:
        print('  tune unavailable (no TX bridge). '); return
    parts = arg.split()
    seconds = 3.0
    watts = None
    try:
        if len(parts) >= 1 and parts[0]:
            seconds = float(parts[0])
        if len(parts) >= 2:
            watts = None if parts[1].lower() in ('cur', 'current', '') else float(parts[1])
    except ValueError:
        print('  usage: tune [seconds] [watts]   (e.g. "tune", "tune 5", "tune 5 3")')
        return
    if seconds <= 0 or seconds > 30:
        print('  tune duration must be 0–30 s'); return
    shown = f'{watts:g} W' if watts is not None else 'current power'
    print(f'  ⚠️  TUNE: keying carrier for {seconds:g}s @ {shown} — antenna/dummy load!')
    ok, msg = br.tune_carrier(seconds=seconds, watts=watts)
    print(f'  {msg}')


def _list_devices():
    """Print available audio devices (sounddevice) + PulseAudio sinks if present."""
    try:
        devs = sd.query_devices()
        print('  sounddevice audio devices:')
        for i, d in enumerate(devs):
            io = []
            if d.get('max_input_channels'):
                io.append('in')
            if d.get('max_output_channels'):
                io.append('out')
            print(f'    [{i}] {d["name"]}  ({"/".join(io) or "?"})')
    except Exception as e:  # noqa: BLE001
        print(f'  (could not query sounddevice: {e})')
    import shutil
    import subprocess
    if shutil.which('pactl'):
        try:
            out = subprocess.run(['pactl', 'list', 'short', 'sinks'],
                                 capture_output=True, text=True, timeout=3).stdout
            names = [ln.split('\t')[1] for ln in out.splitlines() if '\t' in ln]
            if names:
                print('  PulseAudio sinks: ' + ', '.join(names))
        except Exception:  # noqa: BLE001
            pass


def _live_params(rx):
    """Snapshot the current live parameters as a config dict (RX + TX)."""
    ch = rx.channels[0]
    p = {
        'freq_khz': (rx.radio.current_freq or 0) / 1000.0,
        'mode': ch.demod.mode,
        'agc': ch.demod.agc_mode,
        'rit_hz': getattr(ch.demod, 'rit_hz', 0.0),
        'nr': ch.filters.nr.level,
        'nb': ch.filters.nb.level,
        'sql': ch.filters.squelch.level,
    }
    if rx.bridge is not None:
        br = rx.bridge
        p.update({
            'tx_mode': br.tx_mode,
            'tx_mic_gain': br.mic_gain,
            'tx_watts': br.tx_watts,
            'max_power_watts': br.max_power_watts,
        })
    return p


def _read_config(rx):
    """Load the config file and immediately APPLY every parameter to the live
    radio/bridge."""
    from solsdr import config as cfg
    c = cfg.load()
    if not c:
        print('  no config file found (%s)' % cfg.config_path()); return
    applied = []
    try:
        if 'freq_khz' in c:
            rx.tune(float(c['freq_khz'])); applied.append(f"freq={c['freq_khz']}")
        if 'mode' in c:
            rx.set_mode(str(c['mode'])); applied.append(f"mode={c['mode']}")
        if 'agc' in c:
            rx.channels[0].demod.set_agc(str(c['agc'])); applied.append(f"agc={c['agc']}")
        if 'rit_hz' in c:
            rx.channels[0].demod.set_rit(float(c['rit_hz']))
        for k, attr in (('nr', 'nr'), ('nb', 'nb'), ('sql', 'squelch')):
            if k in c:
                getattr(rx.channels[0].filters, attr).level = float(c[k])
        if 'preamp' in c:
            rx.radio.set_preamp(str(c['preamp'])); applied.append(f"preamp={c['preamp']}")
        if rx.bridge is not None:
            br = rx.bridge
            if 'tx_mode' in c:
                br.tx_mode = str(c['tx_mode']).upper()
            if 'tx_mic_gain' in c:
                br.set_mic_gain(float(c['tx_mic_gain']))
            if 'tx_watts' in c:
                br.set_tx_watts(None if c['tx_watts'] in (None, 'none', '') else float(c['tx_watts']))
            if 'max_power_watts' in c:
                br.max_power_watts = None if c['max_power_watts'] in (None, 'none', '') else float(c['max_power_watts'])
            applied.append('tx settings')
    except Exception as e:  # noqa: BLE001
        print(f'  applied some settings, then error: {e}')
    print(f'  read-config: applied from {cfg.config_path()} — {", ".join(applied)}')


def _write_config(rx):
    """Write ALL current live parameters into the config file."""
    from solsdr import config as cfg
    params = _live_params(rx)
    path = cfg.update(params)
    print(f'  write-config: wrote {len(params)} params to {path}')
    for k in sorted(params):
        print(f'    {k} = {params[k]}')


def interactive_loop(rx):
    dual = len(rx.channels) > 1
    _print_help(dual)
    while True:
        try:
            line = input('sdr> ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ('q', 'quit', 'exit'):
            break
        if line in ('help', '?', 'h'):
            _print_help(dual); continue
        if line == 'devices':
            _list_devices(); continue
        if line == 'tx' or line.startswith('tx '):
            _tx_command(rx, line[2:].strip()); continue
        if line == 'tune' or line.startswith('tune '):
            _tune_command(rx, line[4:].strip()); continue
        if line in ('read-config', 'readconfig', 'read_config'):
            _read_config(rx); continue
        if line in ('write-config', 'writeconfig', 'write_config'):
            _write_config(rx); continue
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
            arg = line[3:].strip()
            low = arg.lower()
            first = low.split()[0] if low else ''
            if first == 'pitch':
                hz = float(arg.split()[1])
                rx.channels[target_rx].demod.set_cw(pitch=hz)
                print(f'  RX{target_rx + 1} CW (RX) pitch -> {hz:g} Hz')
            elif first == 'bw':
                hz = float(arg.split()[1])
                rx.channels[target_rx].demod.set_cw(bandwidth=hz)
                print(f'  RX{target_rx + 1} CW (RX) bandwidth -> {hz:g} Hz')
            elif first in ('on', 'off', '1', '0', 'true', 'false'):
                on = first in ('on', '1', 'true')
                rx.cw_decode(on, rx=target_rx)
                print(f'  RX{target_rx + 1} CW decode -> {"ON" if on else "off"}')
            else:
                # anything else is a message to TRANSMIT as CW
                br = getattr(rx, 'bridge', None)
                if br is None:
                    print('  CW send unavailable (no TX bridge)')
                else:
                    print(f'  ⚠️  CW TX: sending {arg!r} — antenna/dummy load!')
                    ok, msg = br.send_cw(arg)
                    print(f'  {msg}')
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
        elif line.startswith('preamp '):
            st = line[7:].strip()
            try:
                rx.radio.set_preamp(st)
                print(f'  preamp/att -> {st}')
            except ValueError as e:
                print(f'  {e}')
        elif line.startswith('rit '):
            hz = float(line[4:])
            rx.channels[target_rx].demod.set_rit(hz)
            print(f'  RX{target_rx + 1} RIT -> {hz:g} Hz')
        elif line.startswith('agc '):
            mode = line[4:].strip().lower()
            rx.channels[target_rx].demod.set_agc(mode)
            print(f'  RX{target_rx + 1} AGC -> {mode}')
        elif line.startswith('gain ') or line.startswith('vol '):
            g = float(line.split(None, 1)[1])
            # audio output level = fixed (linear) gain; implies AGC off
            rx.channels[target_rx].demod.set_agc(f'fixed:{g:g}')
            print(f'  RX{target_rx + 1} audio gain -> {g:g} (AGC off)')
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
                print(f'  ? unknown command: {line!r} — type "help"')


def main():
    from solsdr import __version__
    from solsdr.config import load as load_config
    from solsdr.log import setup_logging
    ap = argparse.ArgumentParser()
    ap.add_argument('--version', action='version',
                    version=f'solsdr {__version__}')
    ap.add_argument('--config', default=None,
                    help='config file (default: ~/.config/solsdr/config.*)')
    ap.add_argument('--log-level', default='info',
                    choices=['debug', 'info', 'warning', 'error'],
                    help='logging verbosity (default info)')
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
    # Text control API is ON BY DEFAULT (loopback :5556) — the panadapter and
    # other clients read it for live freq/mode/S-meter and to send commands.
    # Matches --iq-server's default-on rationale. --no-control-api to disable.
    ap.add_argument('--control-api', dest='control_api', action='store_true',
                    default=True,
                    help='text control API on :5556 (default: ON)')
    ap.add_argument('--no-control-api', dest='control_api', action='store_false',
                    help='disable the text control API (it is on by default)')
    # RX IQ streaming is ON BY DEFAULT — the panadapter, GNU Radio, recorders,
    # etc. all consume it, so binding the port is the normal case. Use
    # --no-iq-server to disable it (e.g. to free the port or reduce load).
    ap.add_argument('--iq-server', dest='iq_server', action='store_true',
                    default=True,
                    help='stream raw complex64 IQ to TCP clients on :5555 '
                         '(default: ON; GNU Radio / panadapter / recorders)')
    ap.add_argument('--no-iq-server', dest='iq_server', action='store_false',
                    help='disable the RX IQ server (it is on by default)')
    ap.add_argument('--iq-port', type=int, default=5555)
    ap.add_argument('--iq-tx-server', action='store_true',
                    help='accept raw complex64 IQ from a TCP client and TRANSMIT '
                         'it (the transmit counterpart to --iq-server). Requires '
                         '--tx-arm to actually key; without it the chain runs '
                         'with NO RF (safe wiring test).')
    ap.add_argument('--iq-tx-port', type=int, default=5558,
                    help='port for the raw-IQ TX server (default 5558)')
    ap.add_argument('--tx-arm', action='store_true',
                    help='ARM the raw-IQ TX server to actually key the radio. '
                         'Off by default. ALWAYS transmit into a dummy load first.')
    ap.add_argument('--tx-watts', type=float, default=None,
                    help='TX output setpoint in watts for the raw-IQ TX server '
                         '(per-band calibration required)')
    ap.add_argument('--max-power-watts', type=float, default=None,
                    help='amp-protection output ceiling in watts for TX '
                         '(clamps drive; refuses to key on an uncalibrated band)')
    # Unified transceiver: the digital-mode / TX bridge (virtual audio + real
    # rigctld + PTT->TXSession) runs IN THIS PROCESS by default, fed by the same
    # Radio via IQ fan-out. This makes solsdr one program that both RX and TX,
    # with the shell controlling everything live. --no-tx drops back to RX-only.
    ap.add_argument('--tx', dest='tx', action='store_true', default=True,
                    help='run the TX/digital-mode bridge in-process (default ON)')
    ap.add_argument('--no-tx', dest='tx', action='store_false',
                    help='RX-only: do not bring up the TX bridge')
    ap.add_argument('--tx-mode', default='USB', help='TX modulation mode (default USB)')
    ap.add_argument('--prefix', default='solsdr',
                    help='PulseAudio device-name prefix for the bridge')
    ap.add_argument('--monitor', metavar='SINK', default=None,
                    help='also play RX+TX audio to this speaker sink')
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

    # Apply config-file values as argparse defaults so explicit CLI args still
    # win (CLI > config file > built-in default). Pre-parse only to learn
    # --config; unknown config keys are ignored with a warning.
    pre, _ = ap.parse_known_args()
    cfg = load_config(pre.config)
    if cfg:
        known = {a.dest for a in ap._actions}
        good = {k: v for k, v in cfg.items() if k in known}
        for k in cfg:
            if k not in known:
                print(f'[config] ignoring unknown key: {k}')
        ap.set_defaults(**good)
    args = ap.parse_args()
    setup_logging(args.log_level)

    rx = AudioReceiver(args.freq_khz, mode=args.mode, device=args.device,
                       local_ip=args.local_ip, radio_ip=args.radio_ip,
                       variant=args.variant, sample_rate=args.rate,
                       ext_ref=args.ext_ref, rx2_khz=args.rx2,
                       rx2_mode=args.rx2_mode, rx2_device=args.rx2_device)
    if not rx.start():
        sys.exit(1)

    servers = []

    # ── Unified transceiver: bring up the TX/digital-mode bridge in-process ──
    # It shares the single Radio (fed by the receiver's IQ fan-out via
    # rx.bridge.feed_iq), stands up virtual audio + a real rigctld, and keys a
    # TXSession on PTT. The shell's `tx ...` commands then control it LIVE. If
    # PulseAudio/rigctld aren't present, we warn and continue RX-only.
    bridge_poller = None
    if args.tx:
        try:
            from solsdr.audio.js8_bridge import JS8AudioBridge
            from solsdr.audio.rigctld_poller import RigctldPoller
            br = JS8AudioBridge(rx.radio, prefix=args.prefix, tx_mode=args.tx_mode,
                                max_power_watts=args.max_power_watts,
                                tx_watts=args.tx_watts, monitor_sink=args.monitor,
                                verbose=False)
            br.start(external_iq=True)          # fed by rx's IQ fan-out
            rx.bridge = br
            bridge_poller = RigctldPoller(rx.radio, ptt_callback=br.set_ptt,
                                          port=args.hamlib_port)
            bridge_poller.start(); servers.append(bridge_poller)
            print(f'TX/bridge ready: virtual audio "{args.prefix}-rx/-tx", '
                  f'CAT rigctld on :{args.hamlib_port}, PTT keys TX. '
                  f'`tx` in the shell for settings.')
        except Exception as e:  # noqa: BLE001
            print(f'TX bridge unavailable ({e}); running RX-only. '
                  f'(Needs PulseAudio + Hamlib rigctld.)')
            rx.bridge = None

    if args.hamlib and not (args.tx and rx.bridge is not None):
        # Standalone CAT (RX-only, or bridge failed): rigctld with a no-op PTT.
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
    if args.iq_tx_server:
        from solsdr.api.iq_tx_server import IQTXServer
        txs = IQTXServer(rx.radio, port=args.iq_tx_port, mode=args.mode,
                         armed=args.tx_arm, watts=args.tx_watts,
                         max_power_watts=args.max_power_watts)
        txs.start(); servers.append(txs)
        state = 'ARMED — will key on connect' if args.tx_arm else 'NO RF (use --tx-arm to key)'
        print(f'raw-IQ TX server on :{args.iq_tx_port} (complex64 @ '
              f'{rx.radio.wire_rate:.0f} Hz) [{state}]')
        if args.tx_arm:
            print('  ⚠️  TX is ARMED. Connect a client and it WILL transmit. '
                  'Dummy load!')

    try:
        if args.seconds:
            time.sleep(args.seconds)
        else:
            interactive_loop(rx)
    finally:
        for s in servers:
            s.stop()
        if rx.bridge is not None:
            try:
                rx.bridge.stop()
            except Exception:  # noqa: BLE001
                pass
        rx.stop()


if __name__ == '__main__':
    main()
