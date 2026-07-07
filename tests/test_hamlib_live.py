#!/usr/bin/env python3
"""
Live compatibility test: validates the HamlibServer against the REAL Hamlib
`rigctl` binary (model 2 = NET rigctl). Skips cleanly if rigctl is not
installed.

This is the test that proves WSJT-X / fldigi / flrig will actually be able to
open and control the rig, since they all use the same netrigctl client code
path (including the strict dump_state capability negotiation).
"""
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solsdr.api.hamlib_compat import HamlibServer


class FakeRadio:
    def __init__(self):
        self.current_freq = 14074000
        self.current_mode = 'USB'

    def set_frequency(self, hz):
        self.current_freq = hz; return True

    def set_mode(self, m):
        self.current_mode = m; return True

    def set_ptt(self, on):
        return True


def main():
    if not shutil.which('rigctl'):
        print("SKIP: rigctl not installed (apt install libhamlib-utils)")
        return 0

    port = 15978
    h = HamlibServer(FakeRadio(), port=port, verbose=False)
    h.start()
    time.sleep(0.4)

    def rc(*args):
        try:
            p = subprocess.run(
                ['rigctl', '-m', '2', '-r', f'127.0.0.1:{port}', *args],
                capture_output=True, text=True, timeout=5)
            return p.stdout.strip()
        except subprocess.TimeoutExpired:
            return 'TIMEOUT'

    try:
        # If dump_state negotiation is wrong, these hang -> 'TIMEOUT'.
        assert rc('f') == '14074000', "get freq failed (rig_open likely hung)"
        assert rc('F', '21074000', 'f') == '21074000', "set/get freq failed"
        m = rc('M', 'CW', '500', 'm')
        assert m.startswith('CW'), f"set/get mode failed: {m!r}"
        assert rc('T', '1', 't') == '1', "set/get ptt failed"
        print("PASS: real rigctl opened the rig and freq/mode/ptt round-trip")
        return 0
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1
    finally:
        h.stop()


if __name__ == '__main__':
    sys.exit(main())
