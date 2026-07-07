"""
TX packet pacer — precise fixed-cadence emission for TX IQ.

The SunSDR2 DAC expects TX IQ packets at a steady cadence (5.12 ms for the PRO:
200 samples / 39062.5 Hz). Jitter above ~1 ms causes "raspy" distorted audio,
so the emission clock must be stable regardless of GC pauses, the GIL, or
scheduler noise.

Design:
  * A dedicated thread blocks on an absolute-interval `timerfd` (CLOCK_MONOTONIC).
    timerfd fires on a kernel timer, so drift and jitter don't accumulate — each
    tick is scheduled against the timer's own clock, not `time.sleep()`.
  * On each tick the thread pulls one packet's worth of samples from a callback
    and sends it. If the callback has nothing ready (underrun) it emits the
    configured underrun packet (silence) so cadence never breaks.
  * Optionally raises the thread to SCHED_FIFO for the tightest jitter (needs
    CAP_SYS_NICE / root; falls back to SCHED_RR, then normal, logging which).

This module ONLY paces and sends — it does not build packets or key PTT. The
caller supplies a `packet_source()` returning bytes (a full wire packet) or
None, and the pacer handles timing. That keeps timing isolated and testable:
it can send to a loopback socket and the jitter can be measured with no radio.

Nothing here transmits by itself; wiring it to the radio's TX port + PTT is a
separate, deliberate step.
"""

import ctypes
import ctypes.util
import os
import socket
import threading
import time
from typing import Callable, Optional

# Nanoseconds helpers
_NS = 1_000_000_000


def _try_realtime(priority: int = 50) -> str:
    """Best-effort raise the CURRENT thread to real-time scheduling.

    Returns a short string describing what was achieved: 'fifo', 'rr', or
    'normal' (no privilege). Uses sched_setscheduler on the calling thread
    (tid 0 = self). Safe to call from the pacer thread.
    """
    if not hasattr(os, 'sched_setscheduler'):
        return 'normal'
    for policy_name in ('SCHED_FIFO', 'SCHED_RR'):
        policy = getattr(os, policy_name, None)
        if policy is None:
            continue
        try:
            param = os.sched_param(priority)
            os.sched_setscheduler(0, policy, param)
            return policy_name.split('_')[1].lower()  # 'fifo' / 'rr'
        except (PermissionError, OSError):
            continue
    return 'normal'


class TXPacer:
    """Paces packet emission at a fixed interval using a timerfd."""

    def __init__(self, interval_s: float, packet_source: Callable[[], Optional[bytes]],
                 send: Callable[[bytes], None], underrun_packet: Optional[bytes] = None,
                 realtime: bool = True, rt_priority: int = 50, verbose: bool = True):
        """
        interval_s: seconds between packets (PRO TX = 200/39062.5 = 0.005120).
        packet_source: called each tick; returns a full wire packet (bytes) to
            send, or None if nothing is ready (-> underrun_packet is sent).
        send: called with the bytes to emit (e.g. sock.sendto bound).
        underrun_packet: bytes sent on underrun to preserve cadence; if None and
            the source returns None, nothing is sent that tick (a gap).
        realtime: attempt SCHED_FIFO/RR on the pacer thread.
        """
        self.interval_s = interval_s
        self.packet_source = packet_source
        self.send = send
        self.underrun_packet = underrun_packet
        self.realtime = realtime
        self.rt_priority = rt_priority
        self.verbose = verbose

        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._timer_fd = -1
        self.sched_policy = 'not-started'

        # stats (populated live; read after stop for a summary)
        self.ticks = 0
        self.underruns = 0
        self.sent = 0
        self._gaps_ns = []          # inter-send gaps for jitter analysis
        self._keep_gaps = True
        self._last_emit_ns = 0

    def _log(self, *a):
        if self.verbose:
            print('[tx-pacer]', *a)

    def start(self):
        if not hasattr(os, 'timerfd_create'):
            raise RuntimeError('os.timerfd_create unavailable; TX pacing needs '
                               'Linux timerfd (present on the radio host)')
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._timer_fd >= 0:
            try:
                os.close(self._timer_fd)
            except OSError:
                pass
            self._timer_fd = -1

    def _run(self):
        if self.realtime:
            self.sched_policy = _try_realtime(self.rt_priority)
            self._log(f'scheduling: {self.sched_policy}')
        else:
            self.sched_policy = 'normal'

        clk = getattr(time, 'CLOCK_MONOTONIC', 1)
        self._timer_fd = os.timerfd_create(clk)
        # Periodic timer: first fire after one interval, then every interval.
        os.timerfd_settime(self._timer_fd, initial=self.interval_s,
                           interval=self.interval_s)

        while self._running:
            try:
                # Blocks until the next tick; returns number of expirations.
                expirations = int.from_bytes(os.read(self._timer_fd, 8), 'little')
            except OSError:
                break
            if not self._running:
                break
            self.ticks += 1
            # If we fell behind (expirations > 1) we still send exactly one
            # packet per logical tick; the caller's source decides catch-up.
            pkt = None
            try:
                pkt = self.packet_source()
            except Exception as e:  # noqa: BLE001
                self._log(f'packet_source error: {e}')
            if pkt is None:
                self.underruns += 1
                pkt = self.underrun_packet
            if pkt is not None:
                now = time.perf_counter_ns()
                if self._last_emit_ns and self._keep_gaps:
                    self._gaps_ns.append(now - self._last_emit_ns)
                self._last_emit_ns = now
                try:
                    self.send(pkt)
                    self.sent += 1
                except OSError as e:
                    self._log(f'send error: {e}')

    # -- jitter reporting --------------------------------------------------
    def gap_stats_ms(self):
        """Return (mean, stdev, min, max, count) of inter-send gaps in ms."""
        if not self._gaps_ns:
            return None
        g = [x / 1e6 for x in self._gaps_ns]
        n = len(g)
        mean = sum(g) / n
        var = sum((x - mean) ** 2 for x in g) / n
        return {
            'mean_ms': mean,
            'stdev_ms': var ** 0.5,
            'min_ms': min(g),
            'max_ms': max(g),
            'max_dev_ms': max(abs(x - mean) for x in g),
            'count': n,
        }
