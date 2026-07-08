"""
Real-time TX chain: audio source -> SSB modulator -> packetizer -> paced UDP.

Streams audio (from a file via ffmpeg, or any callable yielding audio blocks),
modulates it to complex IQ at the radio wire rate in real time, packetizes into
1210-byte TX frames, and emits them at the precise 5.12 ms PRO cadence via the
TXPacer.

SAFETY: this class only produces and paces packets to a destination socket. It
does NOT key PTT. Point `dest` at a loopback address to exercise the full chain
with zero RF (the default test path). Keying the radio is a separate, deliberate
step layered on top later.
"""

import subprocess
import threading
import queue
from typing import Optional

import numpy as np

from .protocol import packet as pk
from .protocol.profiles import get_profile
from .protocol.tx_pacer import TXPacer
from .dsp.modulator import Modulator


def ffmpeg_audio_reader(path, audio_rate=48000):
    """Yield float32 mono audio blocks decoded from any file via ffmpeg.

    Streams (doesn't load the whole file), so it works for long media and
    real-time pacing. Yields ~20 ms blocks.
    """
    block = audio_rate // 50  # 20 ms
    proc = subprocess.Popen(
        ['ffmpeg', '-nostdin', '-loglevel', 'quiet', '-i', path,
         '-f', 'f32le', '-ac', '1', '-ar', str(audio_rate), '-'],
        stdout=subprocess.PIPE)
    try:
        while True:
            raw = proc.stdout.read(block * 4)
            if not raw:
                break
            yield np.frombuffer(raw, dtype=np.float32)
    finally:
        try:
            proc.stdout.close()
            proc.terminate()
        except Exception:
            pass


class RealtimeTX:
    def __init__(self, dest, variant='PRO', mode='USB', audio_rate=48000,
                 realtime=True, verbose=True):
        """
        dest: (ip, port) to send TX IQ packets to. Use a loopback addr for
              no-RF testing. (The real radio TX port comes from the profile.)
        """
        self.profile = get_profile(variant.upper())
        self.wire_rate = self.profile.wire_rate
        self.mode = mode.upper()
        self.audio_rate = audio_rate
        self.dest = dest
        self.realtime = realtime
        self.verbose = verbose

        self.mod = Modulator(audio_rate=audio_rate, wire_rate=self.wire_rate,
                             mode=self.mode)
        # IQ sample ring the modulator fills and the pacer drains, 200 at a time.
        self._iq_buf = np.zeros(0, dtype=np.complex64)
        self._buf_lock = threading.Lock()
        self._seq = 0
        self._silence = pk.encode_iq_packet(
            np.zeros(pk.IQ_SAMPLES_PER_PKT, np.complex64), 0, self.profile.magic)

        self._sock = None
        self._pacer: Optional[TXPacer] = None
        self._feeder: Optional[threading.Thread] = None
        self._running = False
        self.source_exhausted = False

        # stats
        self.packets_sent = 0

    def _log(self, *a):
        if self.verbose:
            from .log import log_line; log_line('tx', ' '.join(str(x) for x in a))

    def _packet_source(self):
        """Called by the pacer each tick: return one 1210-byte TX packet, or
        None (underrun -> pacer sends silence and cadence holds)."""
        with self._buf_lock:
            if len(self._iq_buf) < pk.IQ_SAMPLES_PER_PKT:
                return None
            chunk = self._iq_buf[:pk.IQ_SAMPLES_PER_PKT]
            self._iq_buf = self._iq_buf[pk.IQ_SAMPLES_PER_PKT:]
        pkt = pk.encode_iq_packet(chunk, self._seq, self.profile.magic)
        self._seq = (self._seq + 1) & 0xFFFF
        self.packets_sent += 1
        return pkt

    def _feed_loop(self, audio_iter):
        """Modulate incoming audio blocks into the IQ buffer ahead of the pacer.
        Keeps roughly a small buffer so the pacer never underruns while not
        running unbounded ahead of real time."""
        import time
        target_ahead = int(self.wire_rate * 0.8)  # ~800 ms of IQ buffered
        for audio_block in audio_iter:
            if not self._running:
                break
            iq = self.mod.process(audio_block)
            with self._buf_lock:
                self._iq_buf = np.concatenate([self._iq_buf, iq])
            # throttle: don't decode the whole file at once
            while self._running:
                with self._buf_lock:
                    ahead = len(self._iq_buf)
                if ahead < target_ahead:
                    break
                time.sleep(0.02)
        self.source_exhausted = True
        self._log('audio source exhausted')

    def start(self, audio_iter):
        """Begin real-time modulation + paced emission from an audio iterator."""
        import socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._running = True

        interval = pk.IQ_SAMPLES_PER_PKT / self.wire_rate  # 5.12 ms for PRO
        self._pacer = TXPacer(
            interval, self._packet_source,
            lambda b: self._sock.sendto(b, self.dest),
            underrun_packet=self._silence, realtime=self.realtime,
            verbose=self.verbose)

        self._feeder = threading.Thread(target=self._feed_loop,
                                        args=(audio_iter,), daemon=True)
        self._feeder.start()
        # Pre-buffer a healthy margin of IQ before pacing starts so ffmpeg
        # startup latency / scheduling don't cause initial underruns.
        import time
        need = int(self.wire_rate * 0.5)  # ~0.5 s of IQ
        deadline = time.time() + 3.0
        while time.time() < deadline:
            with self._buf_lock:
                if len(self._iq_buf) >= need:
                    break
            time.sleep(0.02)
        self._pacer.start()
        self._log(f'TX streaming to {self.dest} @ {interval*1000:.3f} ms '
                  f'cadence, mode {self.mode}')

    def stop(self):
        self._running = False
        if self._pacer:
            self._pacer.stop()
        if self._feeder:
            self._feeder.join(timeout=1)
        if self._sock:
            self._sock.close()

    def jitter(self):
        return self._pacer.gap_stats_ms() if self._pacer else None
