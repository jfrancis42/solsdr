"""
Client-facing control API — simple line-based TCP protocol.

Lets any client (scripts, GUIs, GNU Radio companions) control the radio
without speaking the raw SunSDR UDP protocol. One socket, newline-delimited
commands, human-readable replies.

Protocol (case-insensitive commands, one per line):
    freq <hz>            -> OK freq=<hz>
    mode <USB|LSB|AM|FM|CW> -> OK mode=<mode>
    ptt <on|off|0|1>     -> OK ptt=<on|off>
    power <watts>        -> OK power=<watts>
    preamp <-20|-10|0|+10|off|preamp> -> OK preamp=<state>
    rit <hz>             -> OK rit=<hz>   (0 = off)
    squelch|sql <0-1>    -> OK squelch=<lvl>
    agc <auto|on|off|fixed:GAIN> -> OK agc=<mode>
    nr <0-1>             -> OK nr=<lvl>
    smeter               -> OK smeter=<dBFS>
    status               -> OK freq=<hz> mode=<m> ptt=<on|off> power=<w> streaming=<0|1> smeter=<dBFS>
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
                hz = int(args[0])
                if not hasattr(self.radio, 'set_frequency'):
                    return 'ERR unsupported freq'
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
            if cmd == 'nr':
                if not args or not hasattr(self.radio, 'set_nr'):
                    return 'ERR nr requires <0-1>' if not args else 'ERR unsupported nr'
                self.radio.set_nr(float(args[0]))
                return f'OK nr={float(args[0]):g}'
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
                return (f'OK freq={s["freq"]} mode={s["mode"]} '
                        f'ptt={"on" if s["ptt"] else "off"} '
                        f'power={s["power"]} streaming={streaming}{sm_str}')
            return f'ERR unknown command: {cmd}'
        except ValueError as e:
            return f'ERR bad argument: {e}'
        except Exception as e:  # noqa: BLE001 - report anything to client
            return f'ERR {e}'
