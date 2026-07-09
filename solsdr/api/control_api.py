"""
Client-facing control API — simple line-based TCP protocol.

Lets any client (scripts, GUIs, GNU Radio companions) control the radio
without speaking the raw SunSDR UDP protocol. One socket, newline-delimited
commands, human-readable replies.

Protocol (case-insensitive commands, one per line):
    freq <hz>            -> OK freq=<hz>   (absolute; or +<hz>/-<hz> to step
                            relative to the current freq, e.g. `freq +1000`)
    mode <USB|LSB|AM|FM|CW> -> OK mode=<mode>
    ptt <on|off|0|1>     -> OK ptt=<on|off>
    power <watts>        -> OK power=<watts>
    preamp <-20|-10|0|+10|off|preamp> -> OK preamp=<state>
    rit <hz>             -> OK rit=<hz>   (0 = off)
    squelch|sql <0-1>    -> OK squelch=<lvl>
    agc <auto|on|off|fixed:GAIN> -> OK agc=<mode>
    filter <lo> <hi>     -> OK filter_lo=<hz> filter_hi=<hz>
                            (passband as RF offsets from the dial: USB +, LSB −,
                             CW around 0. e.g. USB `filter 300 2700`)
    sharpness <s>        -> OK sharpness=<s>   (SSB skirt: soft|normal|sharp)
    gain <value>         -> OK gain=<value>   (fixed audio gain; implies AGC off)
    nr <0-1>             -> OK nr=<lvl>
    nb <0-1>             -> OK nb=<lvl>
    notch <hz>           -> OK notch=<hz>   (0 = off)
    apf <0-1>            -> OK apf=<lvl>   (audio peak filter; 0 = off)
    smeter               -> OK smeter=<dBFS>
    status               -> OK freq=.. mode=.. ptt=.. power=.. streaming=.. smeter=..
                              agc=.. gain=.. rit=.. nr=.. nb=.. notch=.. apf=..
                              squelch=.. preamp=..
                            (DSP/front-end fields are present only when the
                             backing control object exposes them, so a status
                             reader can MIRROR the live radio state — changes
                             made via the shell or another client show up here.)
    ping                 -> OK pong
    quit                 -> OK bye  (closes connection)

Errors reply:  ERR <message>

The API delegates to a control object with methods set_frequency(hz),
set_mode(str), set_ptt(bool), set_power(watts). Any of these may be absent;
the API reports ERR unsupported for missing capabilities. This keeps the
API decoupled from the concrete radio/mock implementation.
"""

import socket
import threading

VALID_MODES = {'USB', 'LSB', 'AM', 'FM', 'CW'}


class ControlAPIServer:
    def __init__(self, radio, host='127.0.0.1', port=5556, verbose=True):
        """
        radio: object exposing set_frequency/set_mode/set_ptt/set_power and
               optionally attributes current_freq, current_mode, ptt.
        """
        self.radio = radio
        self.host = host
        self.port = port
        self.verbose = verbose
        self._sock = None
        self._running = False
        self._thread = None
        self._clients = []

        # Shadow state for status reporting (updated on each successful set).
        self.state = {'freq': None, 'mode': None, 'ptt': False, 'power': None}

    def _log(self, *a):
        if self.verbose:
            from ..log import log_line; log_line('ctrl-api', ' '.join(str(x) for x in a))

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(5)
        self._sock.settimeout(0.5)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self._log(f'listening on {self.host}:{self.port}')

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        if self._sock:
            self._sock.close()

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._client_loop, args=(conn, addr), daemon=True)
            t.start()
            self._clients.append(t)

    def _client_loop(self, conn, addr):
        self._log(f'client connected {addr}')
        conn.settimeout(1.0)
        buf = b''
        try:
            while self._running:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    reply = self._handle_line(line.decode('utf-8', 'replace').strip())
                    if reply is None:  # quit
                        conn.sendall(b'OK bye\n')
                        return
                    conn.sendall((reply + '\n').encode('utf-8'))
        finally:
            conn.close()
            self._log(f'client disconnected {addr}')

    def handle_command(self, line):
        """Public: process one command string, return the reply string.

        Exposed for unit testing without a socket. Returns None for 'quit'.
        """
        return self._handle_line(line)

    # Attributes appended to `status` when the backing control object exposes
    # them. `fmt` renders the value; a field is skipped if the attr is missing
    # or None, so status stays clean on a partial/mock backend.
    _DSP_STATUS = [
        ('agc', lambda v: str(v)),
        ('gain', lambda v: f'{float(v):g}'),
        ('rit', lambda v: f'{float(v):g}'),
        ('filter_lo', lambda v: f'{float(v):g}'),
        ('filter_hi', lambda v: f'{float(v):g}'),
        ('sharpness', lambda v: str(v)),
        ('nr', lambda v: f'{float(v):g}'),
        ('nb', lambda v: f'{float(v):g}'),
        ('notch', lambda v: f'{float(v):g}'),
        ('apf', lambda v: f'{float(v):g}'),
        ('squelch', lambda v: f'{float(v):g}'),
        ('preamp', lambda v: str(v)),
    ]

    def _dsp_status_fields(self):
        out = []
        for name, fmt in self._DSP_STATUS:
            val = getattr(self.radio, name, None)
            if val is None:
                continue
            try:
                out.append(f'{name}={fmt(val)}')
            except (ValueError, TypeError):
                continue
        return (' ' + ' '.join(out)) if out else ''

    def _handle_line(self, line):
        if not line:
            return 'ERR empty'
        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            if cmd == 'ping':
                return 'OK pong'
            if cmd == 'quit' or cmd == 'exit':
                return None
            if cmd == 'freq':
                if not args:
                    return 'ERR freq requires <hz>'
                if not hasattr(self.radio, 'set_frequency'):
                    return 'ERR unsupported freq'
                tok = args[0]
                if tok[0] in '+-':
                    # relative step in Hz from the current tuned freq
                    cur = getattr(self.radio, 'current_freq', None) or \
                        self.state.get('freq') or 0
                    hz = int(cur) + int(tok)
                else:
                    hz = int(tok)
                ok = self.radio.set_frequency(hz)
                if ok is False:
                    return 'ERR freq set failed'
                self.state['freq'] = hz
                return f'OK freq={hz}'
            if cmd == 'mode':
                if not args:
                    return 'ERR mode requires <mode>'
                mode = args[0].upper()
                if mode not in VALID_MODES:
                    return f'ERR bad mode (valid: {",".join(sorted(VALID_MODES))})'
                if not hasattr(self.radio, 'set_mode'):
                    return 'ERR unsupported mode'
                ok = self.radio.set_mode(mode)
                if ok is False:
                    return 'ERR mode set failed'
                self.state['mode'] = mode
                return f'OK mode={mode}'
            if cmd == 'ptt':
                if not args:
                    return 'ERR ptt requires <on|off>'
                val = args[0].lower() in ('on', '1', 'true')
                if not hasattr(self.radio, 'set_ptt'):
                    return 'ERR unsupported ptt'
                ok = self.radio.set_ptt(val)
                if ok is False:
                    return 'ERR ptt set failed'
                self.state['ptt'] = val
                return f'OK ptt={"on" if val else "off"}'
            if cmd == 'power':
                if not args:
                    return 'ERR power requires <watts>'
                watts = float(args[0])
                if not hasattr(self.radio, 'set_power'):
                    return 'ERR unsupported power'
                ok = self.radio.set_power(watts)
                if ok is False:
                    return 'ERR power set failed'
                self.state['power'] = watts
                return f'OK power={watts:g}'
            if cmd == 'preamp':
                if not args:
                    return 'ERR preamp requires <-20|-10|0|+10|off|preamp>'
                if not hasattr(self.radio, 'set_preamp'):
                    return 'ERR unsupported preamp'
                ok = self.radio.set_preamp(args[0])
                return f'OK preamp={args[0]}' if ok is not False else 'ERR preamp failed'
            if cmd == 'rit':
                if not args:
                    return 'ERR rit requires <Hz> (0=off)'
                if not hasattr(self.radio, 'set_rit'):
                    return 'ERR unsupported rit'
                hz = float(args[0])
                self.radio.set_rit(hz)
                return f'OK rit={hz:g}'
            if cmd == 'squelch' or cmd == 'sql':
                if not args:
                    return 'ERR squelch requires <0-1>'
                if not hasattr(self.radio, 'set_squelch'):
                    return 'ERR unsupported squelch'
                lvl = float(args[0])
                self.radio.set_squelch(lvl)
                return f'OK squelch={lvl:g}'
            if cmd == 'agc':
                if not args:
                    return 'ERR agc requires <auto|on|off|fixed:GAIN>'
                if not hasattr(self.radio, 'set_agc'):
                    return 'ERR unsupported agc'
                self.radio.set_agc(args[0])
                return f'OK agc={args[0]}'
            if cmd == 'filter':
                if len(args) < 2:
                    return 'ERR filter requires <lo_hz> <hi_hz> (RF offsets)'
                if not hasattr(self.radio, 'set_filter'):
                    return 'ERR unsupported filter'
                lo, hi = float(args[0]), float(args[1])
                ok = self.radio.set_filter(lo, hi)
                if ok is False:
                    return 'ERR filter set failed'
                return f'OK filter_lo={lo:g} filter_hi={hi:g}'
            if cmd == 'sharpness' or cmd == 'skirt':
                if not args:
                    return 'ERR sharpness requires <soft|normal|sharp>'
                if not hasattr(self.radio, 'set_sharpness'):
                    return 'ERR unsupported sharpness'
                ok = self.radio.set_sharpness(args[0].lower())
                if ok is False:
                    return 'ERR bad sharpness (soft|normal|sharp)'
                return f'OK sharpness={args[0].lower()}'
            if cmd == 'gain' or cmd == 'vol':
                if not args:
                    return 'ERR gain requires <value>'
                if not hasattr(self.radio, 'set_gain'):
                    return 'ERR unsupported gain'
                g = float(args[0])
                self.radio.set_gain(g)
                return f'OK gain={g:g}'
            if cmd == 'nr':
                if not args or not hasattr(self.radio, 'set_nr'):
                    return 'ERR nr requires <0-1>' if not args else 'ERR unsupported nr'
                self.radio.set_nr(float(args[0]))
                return f'OK nr={float(args[0]):g}'
            if cmd == 'nb':
                if not args or not hasattr(self.radio, 'set_nb'):
                    return 'ERR nb requires <0-1>' if not args else 'ERR unsupported nb'
                self.radio.set_nb(float(args[0]))
                return f'OK nb={float(args[0]):g}'
            if cmd == 'notch':
                if not args or not hasattr(self.radio, 'set_notch'):
                    return ('ERR notch requires <hz> (0=off)' if not args
                            else 'ERR unsupported notch')
                hz = float(args[0])
                self.radio.set_notch(hz)
                return f'OK notch={hz:g}'
            if cmd == 'apf':
                if not args or not hasattr(self.radio, 'set_apf'):
                    return 'ERR apf requires <0-1>' if not args else 'ERR unsupported apf'
                self.radio.set_apf(float(args[0]))
                return f'OK apf={float(args[0]):g}'
            if cmd == 'smeter':
                # Real RX signal level in dBFS. (Note: cannot be pushed over
                # CAT — the dummy rigctld backend rejects L STRENGTH — so it's
                # exposed here for solsdr's own clients.)
                sm = getattr(self.radio, 's_meter', None)
                if sm is None:
                    return 'ERR unsupported smeter'
                return f'OK smeter={float(sm):.1f}'
            if cmd == 'status':
                s = self.state
                streaming = int(getattr(self.radio, 'streaming', 0) or 0)
                sm = getattr(self.radio, 's_meter', None)
                sm_str = f' smeter={float(sm):.1f}' if sm is not None else ''
                # Prefer the radio's LIVE tuned freq/mode over our shadow state.
                # The shadow only tracks changes made THROUGH this API; the radio
                # can also be retuned via the interactive shell, a rigctld/JS8Call
                # client, etc. Reporting radio.current_freq/current_mode keeps a
                # panadapter or other status reader correct after any retune.
                live_f = getattr(self.radio, 'current_freq', None)
                live_m = getattr(self.radio, 'current_mode', None)
                freq = live_f if live_f else s["freq"]
                mode = live_m if live_m else s["mode"]
                # RX DSP / front-end state, so a client can MIRROR (not just
                # set) the radio. Each field is emitted only if the backing
                # control object exposes it (mock radio doesn't), keeping the
                # API decoupled from any concrete implementation.
                extra = self._dsp_status_fields()
                return (f'OK freq={freq} mode={mode} '
                        f'ptt={"on" if s["ptt"] else "off"} '
                        f'power={s["power"]} streaming={streaming}{sm_str}{extra}')
            return f'ERR unknown command: {cmd}'
        except ValueError as e:
            return f'ERR bad argument: {e}'
        except Exception as e:  # noqa: BLE001 - report anything to client
            return f'ERR {e}'
