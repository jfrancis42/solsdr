#!/usr/bin/env python3
"""
solsdr-server — unified daemon.

Ties the pieces together into one process:
    wake/discover -> control connection -> RX IQ stream
    + client-facing control API (TCP text, :5556)
    + a real Hamlib rigctld (dummy backend, :4532) whose freq/mode/PTT are
      mirrored to the radio (control clients talk to genuine Hamlib)

By default it runs against the MOCK radio so the full stack (both client APIs,
IQ streaming, DSP) can be exercised on any machine with no hardware. Pass
--radio <ip> to talk to real hardware, or --wake to broadcast-discover it.

Examples:
    # Offline: full stack against the mock radio
    python3 -m solsdr.server --mock

    # Real hardware, IP known
    python3 -m solsdr.server --radio 10.1.2.3

    # Real hardware, discover via broadcast wake
    python3 -m solsdr.server --wake --local-ip 10.1.2.185
"""
import argparse
import signal
import sys
import threading
import time

from .api.control_api import ControlAPIServer
from .audio.rigctld_poller import RigctldPoller


class _MockRadioAdapter:
    """Adapts MockRadio to the control-object interface the APIs expect."""

    def __init__(self, mock):
        self._mock = mock
        self.current_freq = None
        self.current_mode = 'USB'

    @property
    def streaming(self):
        return 1 if self._mock.streaming else 0

    def set_frequency(self, hz):
        self._mock.freq_hz = hz
        self.current_freq = hz
        return True

    def set_mode(self, mode):
        self.current_mode = mode
        return True

    def set_ptt(self, on):
        self._mock.ptt = bool(on)
        return True

    def set_power(self, watts):
        return True


def run_mock_stack(control_port, hamlib_port, tone):
    from .mock_radio import MockRadio

    mock = MockRadio(bind_ip='127.0.0.1', client_ip='127.0.0.1', tone_hz=tone,
                     verbose=True)
    mock.start()
    adapter = _MockRadioAdapter(mock)

    ctrl = ControlAPIServer(adapter, host='127.0.0.1', port=control_port)
    ham = RigctldPoller(adapter, ptt_callback=adapter.set_ptt,
                        host='127.0.0.1', port=hamlib_port)
    ctrl.start()
    ham.start()

    print("\nsolsdr-server (MOCK) running:")
    print(f"  control API : telnet 127.0.0.1 {control_port}  (try 'status')")
    print(f"  hamlib      : point clients at rigctl -m 2 -r 127.0.0.1:{hamlib_port}")
    print("  Ctrl-C to stop.\n")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        time.sleep(0.5)

    print("\nshutting down...")
    ctrl.stop()
    ham.stop()
    mock.stop()


def run_real_stack(radio_ip, control_port, hamlib_port, wake, local_ip):
    from .protocol.control import SolSDRControl

    if wake:
        from .wake import wake as do_wake
        print("Waking / discovering radio via broadcast...")
        result = do_wake(local_ip=local_ip, timeout=60)
        if not result:
            print("No radio found. Aborting.")
            return 1
        radio_ip = result[0]
        print(f"Radio discovered at {radio_ip}")

    radio = SolSDRControl(radio_ip, variant='PRO')
    print(f"Powering on radio at {radio_ip}...")
    radio.power_on()

    ctrl = ControlAPIServer(radio, host='127.0.0.1', port=control_port)
    ham = RigctldPoller(radio, ptt_callback=radio.set_ptt,
                        host='127.0.0.1', port=hamlib_port)
    ctrl.start()
    ham.start()

    print("\nsolsdr-server running against real hardware:")
    print(f"  control API : 127.0.0.1:{control_port}")
    print(f"  hamlib      : point clients at rigctl -m 2 -r 127.0.0.1:{hamlib_port}")
    print("  Ctrl-C to stop.\n")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    ka = 0
    while not stop.is_set():
        time.sleep(0.5)
        ka += 1
        if ka % 4 == 0:  # ~2s keepalive
            radio.keepalive()

    print("\nshutting down...")
    ctrl.stop()
    ham.stop()
    radio.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description='solsdr-server unified daemon')
    ap.add_argument('--mock', action='store_true',
                    help='run against the built-in mock radio (no hardware)')
    ap.add_argument('--radio', metavar='IP', help='real radio IP (e.g. 10.1.2.3)')
    ap.add_argument('--wake', action='store_true',
                    help='broadcast-discover the radio before connecting')
    ap.add_argument('--local-ip', default='10.1.2.185',
                    help='local interface IP for wake broadcast')
    ap.add_argument('--control-port', type=int, default=5556)
    ap.add_argument('--hamlib-port', type=int, default=4532)
    ap.add_argument('--tone', type=float, default=1000.0,
                    help='mock RX tone offset Hz')
    args = ap.parse_args()

    if args.mock or (not args.radio and not args.wake):
        run_mock_stack(args.control_port, args.hamlib_port, args.tone)
        return 0
    return run_real_stack(args.radio, args.control_port, args.hamlib_port,
                          args.wake, args.local_ip)


if __name__ == '__main__':
    sys.exit(main())
