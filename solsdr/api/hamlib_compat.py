r"""
Hamlib rigctld protocol compatibility layer.

Implements enough of the rigctld TCP protocol that fldigi, WSJT-X, flrig,
and other Hamlib-NET clients can control the SunSDR2 by pointing at
"Hamlib NET rigctl" -> localhost:4532.

rigctld uses short single-letter commands. Lower-case = set, upper-case =
get. Replies to set commands end with a status line "RPRT <code>" (0 = OK,
negative errno on failure). Get commands return the value(s) one per line,
then in extended mode a RPRT line.

Supported subset (the commands WSJT-X / fldigi actually use):
    F <hz>          set frequency          -> RPRT 0
    f               get frequency          -> <hz>
    M <mode> <pb>   set mode + passband    -> RPRT 0
    m               get mode               -> <mode>\n<passband>
    T <0|1>         set PTT                -> RPRT 0
    t               get PTT                -> <0|1>
    V <vfo> / v     set/get VFO            -> RPRT 0 / VFOA
    S <0|1> <vfo>   set split (ignored)    -> RPRT 0
    s               get split              -> 0\nVFOA
    \dump_state     capability dump (WSJT-X issues this on connect)
    \chk_vfo        -> CHKVFO 0
    q / Q           close connection

Mode names map to the radio's set_mode(): USB, LSB, CW, CWR, AM, FM, etc.
"""

import socket
import threading

# Hamlib mode string -> our internal mode. CWR/PKTUSB etc. collapse sensibly.
_HAMLIB_TO_MODE = {
    'USB': 'USB', 'LSB': 'LSB', 'CW': 'CW', 'CWR': 'CW',
    'AM': 'AM', 'FM': 'FM', 'WFM': 'FM',
    'PKTUSB': 'USB', 'PKTLSB': 'LSB', 'DATA': 'USB', 'RTTY': 'LSB',
}
_MODE_TO_HAMLIB = {'USB': 'USB', 'LSB': 'LSB', 'CW': 'CW', 'AM': 'AM', 'FM': 'FM'}
_VFO_TOKENS = {'VFOA', 'VFOB', 'VFOC', 'CURR', 'MEM', 'MAIN', 'SUB',
               'TX', 'RX', 'VFO', 'None'}


def _strip_vfo(args):
    """Long-form split/targetable commands may carry a leading VFO token
    (e.g. 'set_split_mode VFOB USB 3000'); the short forms don't. Drop a
    leading VFO so 'X'/'I' see the value in args[0]."""
    if args and args[0] in _VFO_TOKENS:
        return args[1:]
    return args


RIG_OK = 'RPRT 0'
RIG_EINVAL = 'RPRT -1'
RIG_ENIMPL = 'RPRT -11'


class HamlibServer:
    def __init__(self, radio, host='127.0.0.1', port=4532, verbose=True):
        self.radio = radio
        self.host = host
        self.port = port
        self.verbose = verbose
        self._sock = None
        self._running = False
        self._thread = None

        # Shadow state (rigctld clients poll get-freq/get-mode constantly).
        self.freq = getattr(radio, 'current_freq', None) or 14074000
        self.mode = getattr(radio, 'current_mode', None) or 'USB'
        self.passband = 2400
        self.ptt = 0
        self.split = 0
        # Split TX (JS8Call/WSJT-X use rig split to place the TX signal). We're
        # a single-VFO SDR — the audio offset lives in the modulated audio — so
        # we track these and apply the split TX freq as the dial frequency.
        self.split_freq = self.freq
        self.split_mode = self.mode
        self.split_passband = self.passband

    def _log(self, *a):
        if self.verbose:
            print('[hamlib]', *a)

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(5)
        self._sock.settimeout(0.5)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self._log(f'rigctld-compatible server on {self.host}:{self.port} '
                  f'(use "Hamlib NET rigctl" in WSJT-X/fldigi)')

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
            threading.Thread(target=self._client_loop, args=(conn, addr),
                             daemon=True).start()

    def _client_loop(self, conn, addr):
        self._log(f'client {addr}')
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
                    cmd = line.decode('utf-8', 'replace').strip()
                    if not cmd:
                        continue
                    reply = self.handle_command(cmd)
                    if reply is None:  # quit
                        return
                    if reply != '':
                        conn.sendall((reply + '\n').encode('utf-8'))
        finally:
            conn.close()
            self._log(f'client {addr} closed')

    # -- command dispatch (public for unit testing) ------------------------
    def handle_command(self, cmd):
        """Process one rigctld command line, return reply string.

        Returns None if the connection should close (q/Q).
        """
        # Long (backslash) commands
        if cmd.startswith('\\'):
            return self._handle_long(cmd[1:])

        parts = cmd.split()
        c = parts[0]
        args = parts[1:]

        # set commands (lower except F/M/T/V/S which are upper in rigctld)
        if c == 'F':
            try:
                # rigctl sends freq as a float string, e.g. "21074000.000000"
                self.freq = int(float(args[0]))
            except (IndexError, ValueError):
                return RIG_EINVAL
            if hasattr(self.radio, 'set_frequency'):
                self.radio.set_frequency(self.freq)
            return RIG_OK
        if c == 'f':
            return str(self.freq)
        if c == 'M':
            if len(args) < 1:
                return RIG_EINVAL
            hmode = args[0].upper()
            self.mode = _HAMLIB_TO_MODE.get(hmode, self.mode)
            if len(args) >= 2:
                try:
                    pb = int(args[1])
                    if pb > 0:
                        self.passband = pb
                except ValueError:
                    pass
            if hasattr(self.radio, 'set_mode'):
                self.radio.set_mode(self.mode)
            return RIG_OK
        if c == 'm':
            return f'{_MODE_TO_HAMLIB.get(self.mode, "USB")}\n{self.passband}'
        if c == 'T':
            try:
                self.ptt = 1 if int(args[0]) else 0
            except (IndexError, ValueError):
                return RIG_EINVAL
            if hasattr(self.radio, 'set_ptt'):
                self.radio.set_ptt(bool(self.ptt))
            return RIG_OK
        if c == 't':
            return str(self.ptt)
        if c == 'V':
            return RIG_OK  # single-VFO radio; accept and ignore
        if c == 'v':
            return 'VFOA'
        if c == 'S':
            try:
                self.split = 1 if int(args[0]) else 0
            except (IndexError, ValueError):
                return RIG_EINVAL
            return RIG_OK
        if c == 's':
            return f'{self.split}\nVFOA'
        # Split TX frequency: 'I <freq>' set, 'i' get. WSJT-X/JS8Call use rig
        # split so a conventional radio's TX VFO sits at dial+audio_offset while
        # they transmit a low audio tone. This SDR bridge is single-VFO: the
        # modulator already places the signal where the app put it in the
        # passband, so we ACCEPT & TRACK the split TX freq but must NOT retune
        # the radio (doing so would double-count the offset and shift the
        # signal). The app is kept happy; the dial stays put.
        if c == 'I':
            a = _strip_vfo(args)
            try:
                self.split_freq = int(float(a[0]))
            except (IndexError, ValueError):
                return RIG_EINVAL
            return RIG_OK
        if c == 'i':
            return str(self.split_freq)
        # Split TX mode: 'X [vfo] <mode> <pb>' set, 'x' get. Track only.
        if c == 'X':
            a = _strip_vfo(args)
            if len(a) < 1:
                return RIG_EINVAL
            self.split_mode = _HAMLIB_TO_MODE.get(a[0].upper(), self.split_mode)
            if len(a) >= 2:
                try:
                    pb = int(a[1])
                    if pb > 0:
                        self.split_passband = pb
                except ValueError:
                    pass
            return RIG_OK
        if c == 'x':
            return f'{_MODE_TO_HAMLIB.get(self.split_mode, "USB")}\n{self.split_passband}'
        if c in ('q', 'Q'):
            return None
        # Unknown -> not implemented (Hamlib clients tolerate this)
        return RIG_ENIMPL

    # Long-form (\name) commands that map to a short command letter, so the
    # set/get logic lives in one place. Args are appended after translation.
    _LONG_TO_SHORT = {
        'set_freq': 'F', 'get_freq': 'f',
        'set_mode': 'M', 'get_mode': 'm',
        'set_ptt': 'T', 'get_ptt': 't',
        'set_vfo': 'V', 'get_vfo': 'v',
        'set_split_vfo': 'S', 'get_split_vfo': 's',
        'set_split_freq': 'I', 'get_split_freq': 'i',
        'set_split_mode': 'X', 'get_split_mode': 'x',
    }

    def _handle_long(self, name):
        parts = name.split() if name else ['']
        verb = parts[0]
        if verb == 'chk_vfo':
            # Real rigctld replies with the bare VFO-mode flag (0 = no VFO
            # parameter expected on commands), not the "CHKVFO 0" long form.
            return '0'
        if verb == 'dump_state':
            return self._dump_state()
        if verb == 'get_powerstat':
            # rigctl reads one value line for a get command. 1 = ON.
            return '1'
        if verb == 'set_powerstat':
            return RIG_OK
        short = self._LONG_TO_SHORT.get(verb)
        if short:
            # Re-dispatch through the short-command handler with the same args.
            return self.handle_command(' '.join([short] + parts[1:]))
        return RIG_ENIMPL

    def _dump_state(self):
        """
        rigctld protocol-0 dump_state. WSJT-X / fldigi / rigctl parse this on
        connect to learn the rig's capabilities. The field order, float
        formatting, filter list, and trailing capability bitmasks must match
        exactly what Hamlib's own rigctld emits or rigctl blocks waiting for
        more lines.

        This is captured verbatim from `rigctld -m 1` (the dummy backend) with
        only the freq ranges narrowed to HF+6m. The 0x1ff mode mask is a
        superset (AM|CW|USB|LSB|RTTY|FM|WFM|CWR|RTTYR) so every mode we map is
        accepted, and the six 0xffff... lines are the get/set func & level
        capability bitmasks the dummy advertises.
        """
        lines = [
            '1',                     # protocol version
            '1',                     # rig model (mirror dummy so parsing is safe)
            '0',                     # ITU region
            # RX ranges: start end modes low_power high_power vfo ant
            '100000.000000 60000000.000000 0x1ff -1 -1 0x77e00007 0xf',
            '0 0 0 0 0 0 0',         # end of rx range list
            # TX ranges
            '1800000.000000 54000000.000000 0x1ff 5000 100000 0x77e00007 0xf',
            '0 0 0 0 0 0 0',         # end of tx range list
            # Tuning steps: modes step
            '0x1ff 1',
            '0x1ff 0',
            '0 0',                   # end of tuning steps
            # Filters: modes passband
            '0xc 2400',              # SSB
            '0xc 1800',
            '0xc 3000',
            '0xc 0',
            '0x2 500',               # CW
            '0x2 2400',
            '0x2 50',
            '0x2 0',
            '0x10 300',              # RTTY
            '0x10 2400',
            '0x10 50',
            '0x10 0',
            '0x1 8000',              # AM
            '0x1 2400',
            '0x1 10000',
            '0x20 15000',            # FM
            '0x20 8000',
            '0x40 230000',           # WFM
            '0 0',                   # end of filters
            '9990',                  # max_rit
            '9990',                  # max_xit
            '10000',                 # max_ifshift
            '0',                     # announces
            '10 ',                   # preamp levels (space-terminated)
            '10 20 30 ',             # attenuator levels
            '0xffffffffffffffff',    # has_get_func
            '0xffffffffffffffff',    # has_set_func
            '0xfffffffff7ffffff',    # has_get_level
            '0xffffff7083ffffff',    # has_set_level
            '0xffffffffffffffff',    # has_get_parm
            '0xffffffffffffffbf',    # has_set_parm
            # Extended capability block (key=value). Modern netrigctl clients
            # (Hamlib 4.x) parse these after the bitmasks and require a "done"
            # sentinel; without it rig_open blocks. Values mirror the dummy
            # backend so every client feature negotiation succeeds.
            'vfo_ops=0x7ffffff',
            'ptt_type=0x1',          # RIG_PTT_RIG — PTT via CAT (the T command).
                                     # 0x0 (RIG_PTT_NONE) makes JS8Call/WSJT-X
                                     # believe the rig can't key and throw a
                                     # "rig control error" on transmit.
            'targetable_vfo=0x10c3',
            'has_set_vfo=1',
            'has_get_vfo=1',
            'has_set_freq=1',
            'has_get_freq=1',
            'has_set_conf=1',
            'has_get_conf=1',
            'has_power2mW=1',
            'has_mW2power=1',
            'timeout=0',
            'rig_model=2',
            'done',
        ]
        return '\n'.join(lines) + '\n' + RIG_OK
