"""
High-level SunSDR2 PRO radio interface.

Combines every verified piece into one object:
  * wake/discovery (broadcast probe)
  * power-on (verified PRO init sequence, control socket bound to port 50001)
  * tuning (0x09 primary + 0x08 companion, DDC offset 0)
  * RX IQ streaming on port 50002 at the PRO's 39062.5 Hz native rate
  * the REQUIRED bidirectional TX-silence keepalive (one 1210-byte silence
    packet echoed back per RX packet, or the radio stops streaming after ~8s)
  * a control keepalive thread (0x18)
  * automatic reconnection when the network is interrupted (graduated: a cheap
    re-tune for a brief stall, a full re-wake/re-power-on for sustained loss)

Deliver decoded complex64 IQ to a user callback. The Radio owns the sockets and
threads; call open() then start_stream(callback), and close() when done.

Verified against real hardware receiving FT8 on 2026-07-06.
"""

import socket
import struct
import threading
import time
from typing import Callable, Optional

import numpy as np

from .protocol import packet as pk
from .protocol.control import SolSDRControl
from .protocol.profiles import get_profile
from .wake import wake as wake_radio

# PRO native IQ wire rate. 200 complex samples/packet at ~195 pkt/s.
# Kept as a module constant for backward compatibility; the authoritative
# value is RadioProfile.wire_rate.
PRO_WIRE_RATE = 39062.5

# Connection states (also passed to on_state_change).
STATE_DISCONNECTED = 'disconnected'
STATE_CONNECTING = 'connecting'
STATE_STREAMING = 'streaming'
STATE_RECONNECTING = 'reconnecting'


class Radio:
    # RX socket recv timeout (s) — each timeout is one "no IQ" tick.
    RX_TIMEOUT = 2.0
    # After this many consecutive timeouts, try the cheap re-tune recovery
    # (handles a brief keepalive lapse: the radio just needs its DDC poked).
    RETUNE_AFTER = 2          # ~4 s
    # After this many, treat it as a real network interruption and do a full
    # reconnect (re-wake + re-power-on + rebuild sockets + re-tune).
    RECONNECT_AFTER = 4       # ~8 s

    def __init__(self, radio_ip: Optional[str] = None, local_ip: str = '10.1.2.185',
                 variant: str = 'PRO', verbose: bool = True,
                 auto_reconnect: bool = True, reconnect_max_backoff: float = 30.0,
                 on_state_change: Optional[Callable[[str], None]] = None,
                 sample_rate: Optional[float] = None):
        """radio_ip may be None to force discovery on open().

        auto_reconnect: on sustained IQ loss, keep trying to re-establish the
            link (re-wake, re-power-on, re-tune) until success or close().
        reconnect_max_backoff: cap (s) on the retry backoff between attempts.
        on_state_change: optional callback(state) fired on state transitions.
        sample_rate: IQ rate in Hz. PRO supports 39062.5 / 78125 / 156250 /
            312500 (verified). Defaults to the profile's default (39062.5 PRO).
        """
        self.radio_ip = radio_ip
        self.local_ip = local_ip
        self.variant = variant.upper()
        self.verbose = verbose
        self.auto_reconnect = auto_reconnect
        self.reconnect_max_backoff = reconnect_max_backoff
        self.on_state_change = on_state_change
        self.profile = get_profile(self.variant)
        self.sample_rate = float(sample_rate) if sample_rate else self.profile.wire_rate
        self.wire_rate = self.sample_rate
        self.rx_port = self.profile.rx_stream_port
        self.needs_tx_keepalive = self.profile.rx_needs_tx_keepalive
        if not self.profile.verified:
            self._log(f'WARNING: {self.profile.name} profile is UNVERIFIED against '
                      f'real hardware — values are from the ArtemisSDR reference. '
                      f'It may not work; report results so it can be confirmed.')

        self.ctrl: Optional[SolSDRControl] = None
        self.rx_sock: Optional[socket.socket] = None
        self._running = False
        self._threads = []
        self._tx_seq = 0
        self._callback: Optional[Callable[[np.ndarray], None]] = None
        # Serializes access to the control socket (keepalive thread, RX-loop
        # recovery/reconnect, and set_frequency/set_mode all share it).
        self._ctrl_lock = threading.Lock()

        # Last requested tuning/mode, re-applied after a reconnect (the ctrl
        # object is rebuilt on reconnect and loses its own current_freq).
        self._last_freq: Optional[int] = None
        self._last_mode: Optional[str] = None
        self._last_ext_ref: Optional[bool] = None

        # state + stats
        self.state = STATE_DISCONNECTED
        self.packets_received = 0
        self.last_rx_time = 0.0
        self.reconnect_count = 0
        # Latest supply telemetry (voltage/current/temp), updated from the
        # radio's periodic 0x1F status packets. None until the first arrives.
        self.telemetry = None
        # Set True by a TXSession while keyed: the RX loop stops echoing the
        # 0xFE silence keepalive during TX (the radio is being fed 0xFD TX IQ
        # instead), matching ExpertSDR3 behavior.
        self._tx_active = False

    def _log(self, *a):
        if self.verbose:
            print('[radio]', *a)

    def _set_state(self, state):
        if state != self.state:
            self.state = state
            if self.on_state_change:
                try:
                    self.on_state_change(state)
                except Exception:  # noqa: BLE001
                    pass

    # -- connection establishment -----------------------------------------
    def _connect(self, wake_timeout: int = 30) -> bool:
        """Wake/discover the radio and run the power-on init sequence.

        Reusable by both open() and reconnect. Closes any existing control
        socket first so the source-port-50001 rebind succeeds.
        """
        # Discover if we don't know the IP yet; otherwise still send a wake
        # probe so an idle/dark NIC responds.
        if self.radio_ip is None:
            self._log('discovering radio via broadcast wake...')
            result = wake_radio(local_ip=self.local_ip, timeout=wake_timeout,
                                verbose=self.verbose)
            if not result:
                self._log('no radio found')
                return False
            self.radio_ip = result[0]
            self._log(f'radio at {self.radio_ip}')
        else:
            wake_radio(local_ip=self.local_ip, timeout=wake_timeout,
                       verbose=False)

        # Build the new control object off-lock (power_on does blocking I/O),
        # then publish it under the lock so the keepalive thread sees a clean
        # swap (it skips while ctrl is None).
        with self._ctrl_lock:
            old = self.ctrl
            self.ctrl = None
        if old is not None:
            try:
                old.close()
            except OSError:
                pass

        new_ctrl = SolSDRControl(self.radio_ip, variant=self.variant,
                                  local_ip=self.local_ip,
                                  sample_rate=self.sample_rate)
        ok = new_ctrl.power_on(verbose=self.verbose)
        if not ok:
            try:
                new_ctrl.close()
            except OSError:
                pass
            return False
        with self._ctrl_lock:
            self.ctrl = new_ctrl
        return True

    def open(self, wake_timeout: int = 30) -> bool:
        """Wake/discover the radio and run the power-on init sequence."""
        self._set_state(STATE_CONNECTING)
        ok = self._connect(wake_timeout=wake_timeout)
        self._set_state(STATE_STREAMING if ok else STATE_DISCONNECTED)
        return ok

    def _open_rx_socket(self):
        """(Re)create and bind the RX IQ socket on the stream port."""
        if self.rx_sock is not None:
            try:
                self.rx_sock.close()
            except OSError:
                pass
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        s.bind(('', self.rx_port))
        s.settimeout(self.RX_TIMEOUT)
        self.rx_sock = s

    # -- tuning ------------------------------------------------------------
    def set_frequency(self, freq_hz: int) -> bool:
        self._last_freq = freq_hz
        if not self.ctrl:
            return False
        with self._ctrl_lock:
            ok = self.ctrl.set_frequency(freq_hz)
        if ok:
            self._log(f'tuned {freq_hz/1000:.1f} kHz')
        return ok

    def set_mode(self, mode: str) -> bool:
        self._last_mode = mode
        if not self.ctrl:
            return False
        with self._ctrl_lock:
            return self.ctrl.set_mode(mode)

    def set_reference(self, external: bool) -> bool:
        """Select external 10 MHz (GPSDO) vs internal reference (0x1D)."""
        self._last_ext_ref = external
        if not self.ctrl:
            return False
        with self._ctrl_lock:
            ok = self.ctrl.set_reference(external)
        if ok:
            self._log(f'reference: {"external 10 MHz (GPSDO)" if external else "internal"}')
        return ok

    def set_hf_lpf(self, engaged: bool) -> bool:
        """Engage the HF low-pass filter (True) or auto (False) — 0x1B."""
        if not self.ctrl:
            return False
        with self._ctrl_lock:
            ok = self.ctrl.set_hf_lpf(engaged)
        if ok:
            self._log(f'HF.LPF: {"engaged" if engaged else "auto"}')
        return ok

    def set_vhf_lna(self, on: bool) -> bool:
        """Switch the VHF low-noise amplifier on/off — 0x05 (82/02)."""
        if not self.ctrl:
            return False
        with self._ctrl_lock:
            ok = self.ctrl.set_vhf_lna(on)
        if ok:
            self._log(f'VHF.LNA: {"on" if on else "off"}')
        return ok

    def set_mic_source(self, source) -> bool:
        """Select mic source: 'mic1'/'mic2'/'pc' or int (0/1/2) — 0x21."""
        if not self.ctrl:
            return False
        with self._ctrl_lock:
            ok = self.ctrl.set_mic_source(source)
        if ok:
            self._log(f'mic source: {source}')
        return ok

    @property
    def current_freq(self):
        return self.ctrl.current_freq if self.ctrl else self._last_freq

    @property
    def current_mode(self):
        return self.ctrl.current_mode if self.ctrl else self._last_mode

    @property
    def streaming(self):
        return 1 if (self._running and time.time() - self.last_rx_time < 1.0) else 0

    # -- streaming ---------------------------------------------------------
    def _make_tx_silence(self, seq: int) -> bytes:
        """1210-byte silence packet the client must echo per RX packet."""
        h = bytearray(10)
        h[0] = self.ctrl.magic if self.ctrl else self.profile.magic
        h[1] = 0xFF
        h[2] = 0xFE
        h[3] = 0xFF
        h[4:6] = struct.pack('<H', 1200)
        h[6:8] = struct.pack('<H', seq & 0xFFFF)
        h[8] = 0x01
        h[9] = 0x00
        return bytes(h) + bytes(1200)

    def start_stream(self, callback: Callable[[np.ndarray], None],
                     freq_hz: Optional[int] = None):
        """Begin RX IQ streaming, delivering complex64 arrays to callback.

        Spawns: the RX receive loop (which also echoes the TX-silence keepalive
        and drives reconnection), and a control-keepalive thread. Returns
        immediately; call close() to stop.
        """
        if freq_hz is not None:
            self.set_frequency(freq_hz)

        self._callback = callback
        self._open_rx_socket()
        self._running = True
        self._set_state(STATE_STREAMING)

        t_rx = threading.Thread(target=self._rx_loop, daemon=True)
        t_ka = threading.Thread(target=self._ctrl_keepalive_loop, daemon=True)
        t_rx.start()
        t_ka.start()
        self._threads = [t_rx, t_ka]
        self._log(f'streaming from {self.radio_ip} (wire rate {self.wire_rate:.1f} Hz)')

    def _reconnect(self) -> bool:
        """Full reconnect after sustained loss: re-wake, re-power-on, rebuild
        the RX socket, and re-apply the last tuning. Retries with exponential
        backoff until success or close(). Runs in the RX-loop thread so it
        owns the RX socket lifecycle exclusively.
        """
        self._set_state(STATE_RECONNECTING)
        backoff = 1.0
        attempt = 0
        while self._running:
            attempt += 1
            self._log(f'reconnect attempt {attempt}...')
            try:
                if self._connect():
                    self._open_rx_socket()
                    # Re-apply state the radio forgot on power cycle.
                    if self._last_mode:
                        self.set_mode(self._last_mode)
                    if self._last_freq:
                        self.set_frequency(self._last_freq)
                    if self._last_ext_ref is not None:
                        self.set_reference(self._last_ext_ref)
                    self.reconnect_count += 1
                    self._set_state(STATE_STREAMING)
                    self._log(f'reconnected (attempt {attempt})')
                    return True
            except OSError as e:
                self._log(f'reconnect attempt {attempt} error: {e}')
            except Exception as e:  # noqa: BLE001
                self._log(f'reconnect attempt {attempt} error: {e}')
            # Backoff (interruptible so close() is responsive).
            waited = 0.0
            while waited < backoff and self._running:
                time.sleep(0.2)
                waited += 0.2
            backoff = min(backoff * 2, self.reconnect_max_backoff)
        return False

    def _rx_loop(self):
        consecutive_timeouts = 0
        while self._running:
            try:
                data, _ = self.rx_sock.recvfrom(2048)
                consecutive_timeouts = 0
                if self.state != STATE_STREAMING:
                    self._set_state(STATE_STREAMING)
            except socket.timeout:
                consecutive_timeouts += 1
                self._log(f'no IQ (timeout {consecutive_timeouts})')
                if consecutive_timeouts >= self.RECONNECT_AFTER and self.auto_reconnect:
                    # Sustained loss — assume the network dropped or the radio
                    # went idle/dark. Full reconnect (re-wake handles a dark NIC).
                    self._log('sustained IQ loss — full reconnect')
                    if self._reconnect():
                        consecutive_timeouts = 0
                    continue
                if consecutive_timeouts >= self.RETUNE_AFTER and self.ctrl and self.current_freq:
                    # Brief stall — cheap recovery: poke the DDC by re-tuning.
                    self._log('attempting stream recovery (re-tune)...')
                    try:
                        with self._ctrl_lock:
                            if self.ctrl:
                                self.ctrl.set_frequency(self.current_freq)
                    except OSError:
                        pass  # escalates to full reconnect on the next timeout
                continue
            except OSError:
                # Socket died under us (e.g. interface went down). If we're
                # meant to keep running, reconnect; otherwise exit.
                if self._running and self.auto_reconnect:
                    self._log('RX socket error — full reconnect')
                    if self._reconnect():
                        consecutive_timeouts = 0
                        continue
                break

            iq = pk.decode_iq_packet(data, magic=self.profile.magic)
            if iq is None:
                # Not an IQ frame — could be the periodic 0x1F telemetry
                # (supply V/A + temperature) that also arrives on this port.
                tlm = pk.parse_telemetry(data, magic=self.profile.magic)
                if tlm is not None:
                    self.telemetry = tlm
                continue
            # Echo silence back to keep the RX stream alive (variant-dependent).
            # Suppressed while TX is active — the TX session feeds 0xFD IQ then.
            if self.needs_tx_keepalive and not self._tx_active:
                try:
                    self.rx_sock.sendto(self._make_tx_silence(self._tx_seq),
                                        (self.radio_ip, self.rx_port))
                    self._tx_seq += 1
                except OSError:
                    pass
            self.packets_received += 1
            self.last_rx_time = time.time()
            if self._callback:
                try:
                    self._callback(iq)
                except Exception as e:  # noqa: BLE001
                    self._log(f'callback error: {e}')

    def _ctrl_keepalive_loop(self):
        while self._running:
            # Read ctrl under the lock; it may be None mid-reconnect.
            with self._ctrl_lock:
                ctrl = self.ctrl
                if ctrl is not None:
                    try:
                        ctrl.keepalive()
                    except OSError:
                        pass  # RX loop handles reconnection
            time.sleep(0.3)

    def close(self):
        self._running = False
        self._set_state(STATE_DISCONNECTED)
        for t in self._threads:
            t.join(timeout=1)
        if self.rx_sock:
            try:
                self.rx_sock.close()
            except OSError:
                pass
        with self._ctrl_lock:
            if self.ctrl:
                try:
                    self.ctrl.close()
                except OSError:
                    pass
        self._log(f'closed ({self.packets_received} IQ packets received, '
                  f'{self.reconnect_count} reconnects)')
