#!/usr/bin/env python3
"""
Tests for the client-facing text control API (control_api).

Dispatch is tested directly (no sockets) for speed/determinism. Hamlib CAT is
no longer a hand-rolled server — external software talks to a real rigctld that
solsdr launches and mirrors to the radio (see solsdr/audio/rigctld_poller.py) —
so there is no in-repo rigctld protocol to unit-test here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solsdr.api.control_api import ControlAPIServer


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


if __name__ == '__main__':
    test_control_api()
    print("\nALL API TESTS PASSED")
