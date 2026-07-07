"""
JS8Call / WSJT-X / fldigi audio bridge for the SunSDR2.

Brings up: the radio (RX IQ streaming + TX), a REAL Hamlib rigctld (dummy
backend) for CAT/PTT, and virtual PulseAudio devices carrying demodulated RX
audio and app TX audio.

CAT design: JS8Call talks to a genuine `rigctld -m 1`, so all CAT/PTT/split
handling is Hamlib's own code (no hand-rolled protocol gaps). A poller connects
to that rigctld as a second client and mirrors freq/mode/PTT to the SunSDR2.
This matches the proven hamlib-audio-sidecar architecture.

Point your digital-mode app at:
  * CAT / rig control : Hamlib NET rigctl, 127.0.0.1:<cat-port> (default 4532)
  * Audio input (RX)  : PulseAudio source  <prefix>-rx.monitor  (or <prefix>-rx-mic)
  * Audio output (TX) : PulseAudio sink    <prefix>-tx
  * PTT method        : CAT (rigctl T command) — the bridge keys TX on PTT.

Example:
  python3 -m solsdr.audio --radio 10.1.2.3 --local-ip 10.1.2.185
  python3 -m solsdr.audio --wake --local-ip 10.1.2.185 --prefix solsdr
"""
import argparse
import signal
import sys
import threading
import time

from ..radio import Radio
from .js8_bridge import JS8AudioBridge
from .rigctld_poller import RigctldPoller


def main():
    ap = argparse.ArgumentParser(
        description='SunSDR2 <-> PulseAudio bridge for JS8Call/WSJT-X/fldigi.')
    ap.add_argument('--radio', metavar='IP', help='radio IP (e.g. 10.1.2.3)')
    ap.add_argument('--wake', action='store_true',
                    help='broadcast-discover the radio instead of --radio')
    ap.add_argument('--local-ip', default='10.1.2.185',
                    help='local IP on the radio subnet (default 10.1.2.185)')
    ap.add_argument('--variant', default='PRO', choices=['PRO', 'DX'])
    ap.add_argument('--hamlib-port', type=int, default=4532,
                    help='CAT port for the real rigctld JS8Call connects to')
    ap.add_argument('--rig-model', default='1',
                    help='Hamlib rig model for rigctld -m (default 1 = dummy '
                         'state store; JS8Call drives it, we mirror to the SDR)')
    ap.add_argument('--prefix', default='solsdr',
                    help='PulseAudio device name prefix (default solsdr)')
    ap.add_argument('--audio-rate', type=int, default=48000,
                    help='audio device rate in Hz (default 48000)')
    ap.add_argument('--tx-mode', default='USB',
                    help='initial TX/RX mode until the app sets one (default USB)')
    ap.add_argument('--max-drive', type=int, default=255,
                    help='hard TX drive-byte ceiling 0-255 (default 255)')
    ap.add_argument('--max-power-watts', type=float, default=None,
                    help='amp-protection output ceiling in watts (calibration-'
                         'gated; refuses to key on an uncalibrated band)')
    ap.add_argument('--tx-watts', type=float, default=None,
                    help='TX output setpoint in watts (resolved to a drive byte '
                         'via the per-band cal; clamped to --max-power-watts)')
    ap.add_argument('--monitor', metavar='SINK', default=None,
                    help='play RX audio AND the exact TX audio to this output '
                         'sink (the built-in speaker), to hear glitches. Use a '
                         'PulseAudio sink name, or "default" for the default '
                         'sink. Run "pactl list sinks short" to list them.')
    ap.add_argument('--monitor-gain', type=float, default=0.7,
                    help='speaker-monitor gain 0-1 (default 0.7)')
    ap.add_argument('--freq', type=int, default=None,
                    help='optional initial tune frequency in Hz')
    args = ap.parse_args()

    if not args.radio and not args.wake:
        ap.error('specify --radio <IP> or --wake')

    radio_ip = args.radio
    if args.wake:
        from ..wake import wake as do_wake
        print('Waking / discovering radio via broadcast...')
        result = do_wake(local_ip=args.local_ip, timeout=60)
        if not result:
            print('No radio found. Aborting.')
            return 1
        radio_ip = result[0]
        print(f'Radio discovered at {radio_ip}')

    radio = Radio(radio_ip=radio_ip, local_ip=args.local_ip,
                  variant=args.variant, verbose=True, auto_reconnect=True)
    if not radio.open():
        print('radio open failed')
        return 1
    if args.freq:
        radio.set_frequency(args.freq)
    radio.set_mode(args.tx_mode)

    monitor_sink = args.monitor
    if monitor_sink == 'default':
        import subprocess
        r = subprocess.run(['pactl', 'get-default-sink'],
                           capture_output=True, text=True)
        monitor_sink = r.stdout.strip() or None

    bridge = JS8AudioBridge(radio, prefix=args.prefix,
                            audio_rate=args.audio_rate, tx_mode=args.tx_mode,
                            max_drive=args.max_drive,
                            max_power_watts=args.max_power_watts,
                            tx_watts=args.tx_watts, monitor_sink=monitor_sink,
                            monitor_gain=args.monitor_gain, verbose=True)

    # Real Hamlib rigctld for CAT; poller mirrors freq/mode/PTT to the radio and
    # keys the bridge on PTT edges.
    poller = RigctldPoller(radio, ptt_callback=bridge.set_ptt,
                           host='127.0.0.1', port=args.hamlib_port,
                           model=args.rig_model, verbose=True)

    bridge.start()    # devices + RX stream + mode-hook (set_ptt via poller)
    poller.start()    # launches real rigctld, begins polling

    print('\n' + '=' * 66)
    print('SunSDR2 JS8Call/WSJT-X audio bridge running')
    print('=' * 66)
    print(f'  CAT control : REAL Hamlib rigctld @ 127.0.0.1:{args.hamlib_port}')
    print(f'                (JS8Call: Rig="Hamlib NET rigctl", '
          f'127.0.0.1:{args.hamlib_port})')
    print(f'  RX audio in : PulseAudio "{args.prefix}-rx.monitor" '
          f'(or "{args.prefix}-rx-mic")')
    print(f'  TX audio out: PulseAudio "{args.prefix}-tx"')
    print(f'  PTT         : CAT (rigctl) — keys TX via the bridge')
    if args.max_power_watts is not None:
        print(f'  Max power   : {args.max_power_watts:g} W ceiling (runtime-locked)')
    if args.tx_watts is not None:
        print(f'  TX setpoint : {args.tx_watts:g} W')
    if monitor_sink:
        print(f'  Speaker mon : RX + TX audio -> {monitor_sink} '
              f'(gain {args.monitor_gain:g})')
    print('  Ctrl-C to stop.')
    print('=' * 66 + '\n')

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        time.sleep(0.5)

    print('\nshutting down...')
    poller.stop()
    bridge.stop()
    radio.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
