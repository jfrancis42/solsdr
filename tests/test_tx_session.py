#!/usr/bin/env python3
"""
TX session orchestration test — offline, NO radio, NO RF.

Uses a fake radio/control so no wire commands hit hardware, and a loopback
socket for the IQ. Verifies:
  * unarmed sessions run the full chain (modulate->pace->send) but NEVER key
  * the exact TX-entry command ordering when armed (against the fake ctrl)
  * packets are valid 0xFD TX frames

Requires timerfd (present on the radio host); skips otherwise.
"""
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from solsdr.protocol.profiles import PRO


class FakeCtrl:
    def __init__(self):
        self.calls = []
        self.drive = None

    def set_frequency(self, f):
        self.calls.append(('freq', f)); return True

    def set_ptt(self, on):
        self.calls.append(('ptt', on)); return True

    def set_drive(self, b):
        self.calls.append(('drive', b)); self.drive = b; return True

    def set_pa(self, on):
        self.calls.append(('pa', on)); return True

    def set_config_block(self, tx):
        self.calls.append(('cfg', tx)); return True


class FakeRadio:
    profile = PRO
    wire_rate = PRO.wire_rate
    radio_ip = '127.0.0.1'
    current_freq = 14074000
    _tx_active = False

    def __init__(self):
        self.ctrl = FakeCtrl()


def _audio_iter(seconds=2):
    fs = 48000
    for _ in range(seconds * 50):
        blk = 0.3 * np.sin(2 * np.pi * 1000 * np.arange(fs // 50) / fs)
        yield blk.astype(np.float32)


def _run(armed, pa=False):
    from solsdr.tx_session import TXSession
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(('127.0.0.1', 0)); rx.settimeout(2)
    port = rx.getsockname()[1]
    got = []

    def rxloop():
        while True:
            try:
                d, _ = rx.recvfrom(2048)
            except socket.timeout:
                return
            got.append(d)
    t = threading.Thread(target=rxloop, daemon=True); t.start()

    r = FakeRadio()
    tx = TXSession(r, mode='USB', realtime=False,
                   loopback_dest=('127.0.0.1', port), verbose=False)
    if armed:
        tx.arm(confirm=True)
    tx.enter_tx(_audio_iter(2), raw_drive=40, pa=pa)
    time.sleep(1.5)
    tx.exit_tx()
    time.sleep(0.3)
    rx.close()
    return r.ctrl.calls, got


def test_unarmed_never_keys():
    if not hasattr(os, 'timerfd_create'):
        print('SKIP: timerfd unavailable'); return
    calls, got = _run(armed=False)
    assert ('ptt', True) not in calls, 'SAFETY VIOLATION: keyed while unarmed'
    assert len(got) > 200, f'expected paced packets, got {len(got)}'
    assert got[10][2] == 0xFD, f'not a TX frame: {got[10][2]:#x}'
    print(f'PASS unarmed: {len(got)} TX packets paced, radio NOT keyed')


def test_armed_ordering():
    if not hasattr(os, 'timerfd_create'):
        print('SKIP: timerfd unavailable'); return
    calls, got = _run(armed=True, pa=True)
    # entry ordering: freq reassert -> ptt True -> pa True (drive was pre-set)
    seq = [c for c in calls if c[0] in ('ptt', 'pa')]
    assert ('ptt', True) in calls, 'armed session should key'
    # ptt True must come before pa True
    i_ptt = calls.index(('ptt', True))
    i_pa = calls.index(('pa', True))
    assert i_ptt < i_pa, f'PA enabled before PTT: {calls}'
    # exit ordering: pa False before ptt False
    i_pa_off = calls.index(('pa', False))
    i_ptt_off = calls.index(('ptt', False))
    assert i_pa_off < i_ptt_off, f'exit order wrong: {calls}'
    print(f'PASS armed ordering: {calls}')


def test_power_limit_calibration_aware():
    """The amp-protection watts ceiling must (a) be read-only, (b) clamp all
    requests, and (c) refuse to key on an uncalibrated band."""
    import os as _os
    from solsdr.tx_session import TXSession
    from solsdr.dsp.tx_power import TXPowerCal

    calpath = '/tmp/_test_cal_safety.json'
    if _os.path.exists(calpath):
        _os.remove(calpath)
    cal = TXPowerCal(path=calpath)
    r = FakeRadio()
    tx = TXSession(r, max_power_watts=5.0, power_cal=cal, verbose=False)

    # read-only ceiling
    assert tx.max_power_watts == 5.0
    try:
        tx.max_power_watts = 100
        assert False, 'max_power_watts must have no setter'
    except AttributeError:
        pass

    # uncalibrated band -> not permitted, drive floored
    ok, _ = tx.tx_permitted()
    assert not ok, 'must refuse to key with amp limit on uncalibrated band'
    tx.set_power_watts(5.0)
    assert r.ctrl.drive <= 8, f'uncal drive not floored: {r.ctrl.drive}'

    # calibrate and verify clamp honors the measured curve
    cal.add_measurement('20m', 40, 3.0)
    cal.add_measurement('20m', 60, 8.0)
    ok, _ = tx.tx_permitted()
    assert ok, 'should permit on calibrated band'
    tx.set_power_watts(50)  # way over the 5 W limit
    w, _ = cal.drive_to_watts(r.ctrl.drive, 14074000)
    assert w <= 5.01, f'clamp exceeded amp limit: {w} W'
    # raw-drive bypass also clamped
    tx.set_drive_raw(255)
    w2, _ = cal.drive_to_watts(r.ctrl.drive, 14074000)
    assert w2 <= 5.01, f'raw bypass exceeded amp limit: {w2} W'
    _os.remove(calpath)
    print('PASS power limit: read-only, calibration-gated, clamps watts + raw')


def _iq_iter(seconds=2, wire_rate=PRO.wire_rate):
    """Yield complex baseband IQ blocks at the wire rate (a pure carrier at
    +1 kHz baseband, amplitude 0.5)."""
    n = int(wire_rate / 50)  # ~20 ms blocks
    ph = 0.0
    dphi = 2 * np.pi * 1000.0 / wire_rate
    for _ in range(seconds * 50):
        idx = np.arange(n)
        blk = 0.5 * np.exp(1j * (ph + dphi * idx))
        ph = (ph + dphi * n) % (2 * np.pi)
        yield blk.astype(np.complex64)


def test_raw_iq_tx_passthrough():
    """iq_input=True must transmit the supplied complex samples verbatim
    (gain+clip only) — no modulation, no resampling — as valid 0xFD frames."""
    if not hasattr(os, 'timerfd_create'):
        print('SKIP: timerfd unavailable'); return
    from solsdr.tx_session import TXSession
    from solsdr.protocol import packet as pk

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(('127.0.0.1', 0)); rx.settimeout(2)
    port = rx.getsockname()[1]
    got = []

    def rxloop():
        while True:
            try:
                d, _ = rx.recvfrom(2048)
            except socket.timeout:
                return
            got.append(d)
    t = threading.Thread(target=rxloop, daemon=True); t.start()

    r = FakeRadio()
    tx = TXSession(r, mode='USB', realtime=False,
                   loopback_dest=('127.0.0.1', port), verbose=False)
    tx.arm(confirm=True)
    tx.enter_tx(_iq_iter(2), raw_drive=40, iq_input=True)
    time.sleep(1.5)
    tx.exit_tx()
    time.sleep(0.3)
    rx.close()

    # find a non-silence TX frame and decode it; its magnitude should be ~0.5
    # (our input amplitude), proving verbatim pass-through, not modulation.
    found = None
    for d in got:
        if len(d) == 1210 and d[2] == 0xFD:
            iq = pk.decode_iq_packet(d, PRO.magic)
            if np.max(np.abs(iq)) > 0.1:
                found = iq
                break
    assert found is not None, 'no non-silent raw-IQ TX frame received'
    peak = float(np.max(np.abs(found)))
    assert 0.4 < peak < 0.6, f'raw IQ amplitude not preserved: peak={peak:.3f}'
    print(f'PASS raw-IQ passthrough: {len(got)} frames, peak={peak:.3f} (~0.5 in)')


def test_raw_iq_tx_gain_and_clip():
    """tx_iq_gain scales input; clip keeps |IQ| <= ~0.98 so 24-bit never wraps."""
    from solsdr.tx_session import TXSession
    r = FakeRadio()
    tx = TXSession(r, realtime=False, verbose=False)
    # gain applied
    tx.tx_iq_gain = 2.0
    out = tx._prep_iq(np.full(10, 0.25 + 0j, dtype=np.complex64))
    assert np.allclose(np.abs(out), 0.5, atol=1e-3), np.abs(out)[:3]
    # clip: huge input pinned under the packing ceiling
    tx.tx_iq_gain = 1.0
    out2 = tx._prep_iq(np.full(10, 5.0 + 0j, dtype=np.complex64))
    assert float(np.max(np.abs(out2))) <= 0.98 + 1e-6
    print('PASS raw-IQ gain + clip')


if __name__ == '__main__':
    test_unarmed_never_keys()
    test_armed_ordering()
    test_power_limit_calibration_aware()
    test_raw_iq_tx_passthrough()
    test_raw_iq_tx_gain_and_clip()
    print('\nTX SESSION TESTS PASSED')
