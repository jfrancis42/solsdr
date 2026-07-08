"""
Virtual PulseAudio devices for the SunSDR2 audio bridge.

Creates two null sinks:
  <prefix>-rx : we write demodulated RX audio here; client apps (JS8Call,
                WSJT-X, fldigi) read it from <prefix>-rx.monitor (or the
                <prefix>-rx-mic remap source for Qt6 apps that hide monitors).
  <prefix>-tx : client apps write TX audio here; we read <prefix>-tx.monitor
                and hand it to the modulator.

Adapted from hamlib-audio-sidecar's Linux PulseAudio backend (MIT). The
device.class=sound / media.class=Audio/Source tagging and the dedicated
parec-reader thread are the hard-won bits: they make JS8Call 3.x (Qt6
Multimedia) actually enumerate the devices, and keep TX audio from pulsing at
the read cadence.
"""
import shutil
import subprocess
import threading

import numpy as np


class PulseAudioDevices:
    """Manages the RX/TX virtual sinks and the pacat/parec pipes."""

    def __init__(self, prefix: str = 'solsdr', audio_rate: int = 48000,
                 verbose: bool = True):
        self.prefix = prefix
        self.audio_rate = int(audio_rate)
        self.verbose = verbose
        self.rx_sink_name = f'{prefix}-rx'
        self.tx_sink_name = f'{prefix}-tx'
        self.rx_input_name = f'{prefix}-rx-mic'
        self._mods = []          # loaded module IDs, unloaded in reverse
        self.pacat_proc = None   # writer: us -> RX sink
        self.parec_proc = None   # reader: TX sink monitor -> us
        self.running = False
        # TX capture buffer, filled by a dedicated reader thread (see
        # reference notes: inline reads pulse at the consume cadence).
        self._tx_buf = bytearray()
        self._tx_lock = threading.Lock()
        self._tx_thread = None

    def _log(self, msg):
        if self.verbose:
            from ..log import log_line; log_line('pulse', msg)

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        for tool in ('pactl', 'pacat', 'parec'):
            if shutil.which(tool) is None:
                raise RuntimeError(
                    f'required tool not found: {tool} (install pulseaudio-utils)')
        r = subprocess.run(['pactl', 'info'], capture_output=True,
                           text=True, timeout=5)
        if r.returncode != 0:
            raise RuntimeError('pactl could not reach the audio server')

        # Clear any stale sinks from a previous run.
        self._unload_by_sink(self.rx_sink_name)
        self._unload_by_sink(self.tx_sink_name)

        # RX sink (we -> apps). Mono is fine for digital modes; stereo would
        # only matter for WFM which JS8Call never uses. Keep it mono to halve
        # the data and match JS8Call's expectations.
        self._mods.append(self._null_sink(
            self.rx_sink_name, f'{self.prefix}_RX_Audio'))
        # TX sink (apps -> we).
        self._mods.append(self._null_sink(
            self.tx_sink_name, f'{self.prefix}_TX_Audio'))
        # Remap RX monitor to a real source for Qt6 apps (JS8Call 3.x).
        self._mods.append(self._remap_source(
            self.rx_input_name, f'{self.rx_sink_name}.monitor',
            f'{self.prefix}_RX_Input'))

        self.pacat_proc = subprocess.Popen(
            ['pacat', '--playback', f'--device={self.rx_sink_name}',
             f'--rate={self.audio_rate}', '--channels=1', '--format=s16le',
             '--latency-msec=200'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        # TX capture: LOW latency. This is real-time transmit audio — a big
        # buffer here adds dead delay between the app keying and RF, and lets
        # stale audio pile up. 20 ms keeps the app-to-radio path tight.
        self.parec_proc = subprocess.Popen(
            ['parec', f'--device={self.tx_sink_name}.monitor',
             f'--rate={self.audio_rate}', '--channels=1', '--format=s16le',
             '--latency-msec=20'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        self.running = True
        self._tx_thread = threading.Thread(target=self._tx_reader_loop,
                                           daemon=True, name='parec-reader')
        self._tx_thread.start()

        self._log(f'RX audio: read {self.rx_sink_name}.monitor '
                  f'(or {self.rx_input_name})')
        self._log(f'TX audio: write {self.tx_sink_name}')

    def stop(self):
        self.running = False
        for proc in (self.pacat_proc, self.parec_proc):
            if proc:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
        if self._tx_thread is not None:
            self._tx_thread.join(timeout=1.0)
            self._tx_thread = None
        # Unload in reverse (remap source depends on the RX null sink).
        for mod in reversed(self._mods):
            subprocess.run(['pactl', 'unload-module', mod],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._mods = []

    # -- RX: we write demodulated audio out ------------------------------
    def write_rx(self, audio: np.ndarray):
        """Write one block of real float32 audio (~[-1,1]) to the RX sink."""
        if not self.running or self.pacat_proc is None:
            return
        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype('<i2')
        try:
            self.pacat_proc.stdin.write(pcm.tobytes())
            self.pacat_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    # -- TX: apps write audio, we read it --------------------------------
    def _tx_reader_loop(self):
        max_bytes = self.audio_rate * 2  # ~1 s of mono s16le
        while self.running and self.parec_proc is not None:
            try:
                data = self.parec_proc.stdout.read(4096)
            except OSError:
                break
            if not data:
                break
            with self._tx_lock:
                self._tx_buf.extend(data)
                if len(self._tx_buf) > max_bytes:
                    del self._tx_buf[:len(self._tx_buf) - max_bytes]

    def read_tx(self, n_samples):
        """Return exactly n_samples of float32 mono audio, or None if fewer
        are buffered. Never blocks."""
        if not self.running:
            return None
        need = n_samples * 2  # s16le mono
        with self._tx_lock:
            if len(self._tx_buf) < need:
                return None
            data = bytes(self._tx_buf[:need])
            del self._tx_buf[:need]
        pcm = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32767.0
        return pcm

    def flush_tx(self):
        """Drop buffered TX audio (call on PTT-on edge so stale pre-key audio
        is never transmitted)."""
        with self._tx_lock:
            self._tx_buf.clear()

    def tx_backlog_samples(self):
        with self._tx_lock:
            return len(self._tx_buf) // 2

    # -- pactl helpers ----------------------------------------------------
    def _null_sink(self, sink_name, description):
        r = subprocess.run([
            'pactl', 'load-module', 'module-null-sink',
            f'sink_name={sink_name}',
            f'sink_properties=device.description="{description}" '
                'device.class=sound device.icon_name=audio-card',
            f'rate={self.audio_rate}', 'channels=1', 'format=s16le',
        ], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f'failed to create sink {sink_name}: {r.stderr}')
        return r.stdout.strip()

    def _remap_source(self, source_name, master, description):
        r = subprocess.run([
            'pactl', 'load-module', 'module-remap-source',
            f'source_name={source_name}', f'master={master}',
            f'source_properties=device.description="{description}" '
                'device.class=sound media.class=Audio/Source '
                'device.icon_name=audio-input-microphone',
        ], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f'failed to remap {source_name}: {r.stderr}')
        return r.stdout.strip()

    def _unload_by_sink(self, sink_name):
        r = subprocess.run(['pactl', 'list', 'modules', 'short'],
                           capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if f'sink_name={sink_name}' in line:
                subprocess.run(['pactl', 'unload-module', line.split()[0]],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)


class SpeakerMonitor:
    """Plays mono float32 audio blocks to a real output sink (the built-in
    speaker) so the operator can hear RX and/or the exact TX audio the bridge
    is sending to the modulator. A single pacat writer; play() is fire-and-
    forget and never blocks the audio path."""

    def __init__(self, sink, audio_rate=48000, gain=0.7, verbose=True):
        self.sink = sink            # PulseAudio sink NAME (or None to disable)
        self.audio_rate = int(audio_rate)
        self.gain = float(gain)     # keep monitor below full-scale
        self.verbose = verbose
        self.proc = None
        self.running = False

    def start(self):
        if not self.sink:
            return
        if shutil.which('pacat') is None:
            if self.verbose:
                print('[monitor] pacat missing — monitor disabled')
            return
        cmd = ['pacat', '--playback', f'--device={self.sink}',
               f'--rate={self.audio_rate}', '--channels=1', '--format=s16le',
               '--latency-msec=150', '--stream-name=solsdr-monitor']
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.running = True
        if self.verbose:
            print(f'[monitor] speaker monitor -> {self.sink}')

    def play(self, audio: np.ndarray):
        if not self.running or self.proc is None or audio is None or not len(audio):
            return
        pcm = np.clip(audio * self.gain, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype('<i2')
        try:
            self.proc.stdin.write(pcm.tobytes())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def stop(self):
        self.running = False
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
            self.proc = None
