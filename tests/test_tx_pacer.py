#!/usr/bin/env python3
"""
TX pacer test (offline, no privilege, no radio).

Verifies the pacer emits at the target cadence, handles underruns without
breaking cadence, and reports jitter. Skips cleanly if timerfd is unavailable
(e.g. non-Linux or an older Python) since TX only runs on the radio host.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_pacer_cadence():
    if not hasattr(os, 'timerfd_create'):
        print('SKIP: os.timerfd_create unavailable on this host')
        return

    from solsdr.protocol.tx_pacer import TXPacer

    interval = 0.00512  # PRO TX cadence
    sent = []

    def source():
        return b'x' * 16  # a stand-in packet

    def send(b):
        sent.append(time.perf_counter())

    # Normal scheduling (CI/unprivileged); still expect well under budget.
    pacer = TXPacer(interval, source, send, realtime=False, verbose=False)
    pacer.start()
    time.sleep(2.0)
    pacer.stop()

    assert pacer.sent > 300, f'expected ~390 packets, got {pacer.sent}'
    st = pacer.gap_stats_ms()
    assert st is not None
    # Non-real-time budget: mean within 1% and max deviation under 2 ms.
    assert abs(st['mean_ms'] - interval * 1000) < 0.05, st
    assert st['max_dev_ms'] < 2.0, f'jitter too high (non-RT): {st}'
    print(f'PASS cadence: mean={st["mean_ms"]:.4f}ms stdev={st["stdev_ms"]:.4f}ms '
          f'max_dev={st["max_dev_ms"]:.4f}ms over {st["count"]} gaps')


def test_pacer_underrun():
    if not hasattr(os, 'timerfd_create'):
        print('SKIP: timerfd unavailable')
        return
    from solsdr.protocol.tx_pacer import TXPacer

    underrun = b'SILENCE'
    got = []

    # source always returns None -> every tick is an underrun -> underrun_packet
    pacer = TXPacer(0.00512, lambda: None, lambda b: got.append(b),
                    underrun_packet=underrun, realtime=False, verbose=False)
    pacer.start()
    time.sleep(1.0)
    pacer.stop()

    assert pacer.underruns > 100, f'expected underruns, got {pacer.underruns}'
    assert all(g == underrun for g in got), 'underrun packet not sent'
    assert pacer.sent == pacer.underruns, 'cadence should hold via underrun pkt'
    print(f'PASS underrun: {pacer.underruns} underruns, cadence held with silence')


if __name__ == '__main__':
    test_pacer_cadence()
    test_pacer_underrun()
    print('\nTX PACER TESTS PASSED')
