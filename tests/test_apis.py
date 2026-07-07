#!/usr/bin/env python3
"""
Tests for the client-facing APIs: control_api (text protocol) and
hamlib_compat (rigctld protocol).

Dispatch is tested directly (no sockets) for speed/determinism. A separate
live test (test_hamlib_live.py) validates against the real `rigctl` binary
when it is installed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solsdr.api.control_api import ControlAPIServer
from solsdr.api.hamlib_compat import HamlibServer


class FakeRadio:
    def __init__(self):
        self.current_freq = None
        self.current_mode = 'USB'
        self.streaming = 0
        self.log = []

    def set_frequency(self, hz):
        self.log.append(('freq', hz)); self.current_freq = hz; return True

    def set_mode(self, m):
        self.log.append(('mode', m)); self.current_mode = m; return True

    def set_ptt(self, on):
        self.log.append(('ptt', on)); return True

    def set_power(self, w):
        self.log.append(('power', w)); return True


def test_control_api():
    r = FakeRadio()
    api = ControlAPIServer(r, verbose=False)
    assert api.handle_command('ping') == 'OK pong'
    assert api.handle_command('freq 14074000') == 'OK freq=14074000'
    assert api.handle_command('mode usb') == 'OK mode=USB'
    assert api.handle_command('mode bogus').startswith('ERR bad mode')
    assert api.handle_command('ptt on') == 'OK ptt=on'
    assert api.handle_command('ptt off') == 'OK ptt=off'
    assert api.handle_command('power 50') == 'OK power=50'
    assert api.handle_command('status').startswith('OK freq=14074000 mode=USB')
    assert api.handle_command('freq') == 'ERR freq requires <hz>'
    assert api.handle_command('bogus').startswith('ERR unknown')
    assert api.handle_command('quit') is None
    print("PASS control_api dispatch")


def test_hamlib_dispatch():
    r = FakeRadio()
    h = HamlibServer(r, verbose=False)
    assert h.handle_command('F 7074000') == 'RPRT 0'
    assert h.handle_command('f') == '7074000'
    assert h.handle_command('F 21074000.000000') == 'RPRT 0'   # rigctl sends floats
    assert h.handle_command('f') == '21074000'
    assert h.handle_command('M USB 2400') == 'RPRT 0'
    assert h.handle_command('m') == 'USB\n2400'
    assert h.handle_command('M PKTUSB 3000') == 'RPRT 0'       # data -> USB
    assert r.current_mode == 'USB'
    assert h.handle_command('T 1') == 'RPRT 0'
    assert h.handle_command('t') == '1'
    assert h.handle_command('v') == 'VFOA'
    assert h.handle_command('V VFOA') == 'RPRT 0'
    assert h.handle_command('s') == '0\nVFOA'
    assert h.handle_command('\\chk_vfo') == '0'
    ds = h.handle_command('\\dump_state')
    assert ds.endswith('RPRT 0')
    assert 'done' in ds                                        # extended cap block
    assert h.handle_command('q') is None
    print("PASS hamlib dispatch")


if __name__ == '__main__':
    test_control_api()
    test_hamlib_dispatch()
    print("\nALL API TESTS PASSED")
