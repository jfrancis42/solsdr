"""
Centralized logging for solsdr.

The package's classes each have a small ``_log()`` method that historically did
``print()``. They now route through ``solsdr.log.get_logger(tag)`` so output has
levels, timestamps, and can be quieted or redirected — while a bare-console
fallback preserves the old look when logging isn't configured.

Usage in a class:

    from .log import get_logger
    self._logger = get_logger('radio')
    ...
    self._logger.info('tuned %.1f kHz', khz)

Or via the compatibility shim the classes use:

    from .log import log_line
    log_line('radio', 'tuned 14074.0 kHz')     # respects the global level

Applications configure it once:

    from solsdr.log import setup_logging
    setup_logging(level='info')          # or 'debug' / 'warning' / a logging.*
"""
import logging
import sys

_CONFIGURED = False
_ROOT = 'solsdr'


def setup_logging(level='info', stream=sys.stderr, fmt=None):
    """Configure solsdr logging once. level: name ('debug'/'info'/'warning'/
    'error') or a logging.* int. Idempotent."""
    global _CONFIGURED
    lg = logging.getLogger(_ROOT)
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    lg.setLevel(level)
    # Reset handlers so re-calling with a new level/stream takes effect.
    for h in list(lg.handlers):
        lg.removeHandler(h)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(
        fmt or '%(asctime)s %(name)s %(message)s', datefmt='%H:%M:%S'))
    lg.addHandler(handler)
    lg.propagate = False
    _CONFIGURED = True
    return lg


def get_logger(tag):
    """Return a child logger 'solsdr.<tag>'."""
    return logging.getLogger(f'{_ROOT}.{tag}')


def log_line(tag, msg, level=logging.INFO):
    """Compatibility shim for the classes' _log() methods.

    If logging has been configured via setup_logging(), route through it.
    Otherwise fall back to the historical bare-console format (``[tag] msg``)
    so nothing regresses for callers that never set logging up and just relied
    on their own ``verbose`` flag.
    """
    if _CONFIGURED:
        get_logger(tag).log(level, '%s', msg)
    else:
        print(f'[{tag}] {msg}')
