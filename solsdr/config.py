"""
Station config file for solsdr.

Loads defaults from ``~/.config/solsdr/config.*`` so users don't retype
``--radio``, ``--local-ip``, ``--device``, etc. every run. CLI arguments always
override the config file.

Format is auto-detected by extension: JSON (`.json`), or a simple `key = value`
INI-style/flat file (`.conf`/`.ini`/`.cfg` — no sections needed). Example
`~/.config/solsdr/config.conf`:

    radio_ip   = 192.0.2.10
    local_ip   = 192.0.2.20
    device     = 5
    variant    = PRO
    freq_khz   = 14074
    mode       = USB

Keys mirror the argparse dest names in solsdr.cli. Unknown keys are
ignored (with a warning) so a config can carry extras.
"""
import json
import os

CONFIG_DIR = os.path.expanduser('~/.config/solsdr')
# Search order — first existing wins.
_CANDIDATES = ['config.json', 'config.conf', 'config.ini', 'config.cfg']


def _parse_flat(text):
    out = {}
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip()] = v.strip()
    return out


def _coerce(v):
    """Turn a string value into int/float/bool where obvious (flat-file only)."""
    if not isinstance(v, str):
        return v
    low = v.lower()
    if low in ('true', 'yes', 'on'):
        return True
    if low in ('false', 'no', 'off'):
        return False
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def config_path():
    """Path of the existing config file, or the default JSON path to create."""
    for c in _CANDIDATES:
        p = os.path.join(CONFIG_DIR, c)
        if os.path.exists(p):
            return p
    return os.path.join(CONFIG_DIR, 'config.json')


def load(path=None):
    """Return a dict of config keys, or {} if no config file. If `path` is
    given, load exactly that file; otherwise search the standard location."""
    if path:
        paths = [path]
    else:
        paths = [os.path.join(CONFIG_DIR, c) for c in _CANDIDATES]
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                text = f.read()
            if p.endswith('.json'):
                return dict(json.loads(text))
            return {k: _coerce(v) for k, v in _parse_flat(text).items()}
        except (OSError, ValueError) as e:
            print(f'[config] failed to read {p}: {e}')
            return {}
    return {}


def update(changes, path=None):
    """Merge `changes` into the existing config and persist it. Returns the
    written path. Preserves keys already present; used by the shell's `tx`
    settings so the TX paths (`solsdr.audio`, the IQ-TX server) pick them up.

    Flat (`key = value`) files are preserved as flat; everything else (and new
    files) is written as JSON so values keep their types."""
    p = path or config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    current = load(p) if os.path.exists(p) else {}
    current.update({k: v for k, v in changes.items() if v is not None})
    if p.endswith('.json'):
        with open(p, 'w') as f:
            json.dump(current, f, indent=2, sort_keys=True)
            f.write('\n')
    else:
        # rewrite the flat file, keeping it human-editable
        with open(p, 'w') as f:
            f.write('# solsdr config (managed; hand-editable)\n')
            for k in sorted(current):
                f.write(f'{k} = {current[k]}\n')
    return p
