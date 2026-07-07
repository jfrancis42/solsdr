"""
Verified SunSDR2 PRO power-on / init sequence.

The canonical data now lives in profiles.py (RadioProfile.init_sequence); this
module re-exports the PRO values for backward compatibility and convenience.

Captured live on 2026-07-06 (expert_14074.pcapng) while ExpertSDR3 was
receiving FT8 on 14074 kHz on a real PRO. See profiles.py for the DX-forward
design rationale.

Hard-won facts (see also profiles.py):
  * Control socket MUST bind source port 50001, or the radio ignores commands.
  * Frequency: 0x09 (primary) then 0x08 (companion); PRO offset is 0.
  * RX stream is bidirectional on 50002 — echo a silence packet per RX packet
    or the radio stops streaming after ~8 s.
"""
from .profiles import PRO

PRO_INIT_SEQUENCE = PRO.init_sequence

# Keepalive packet (0x18) — send periodically on the control socket.
PRO_KEEPALIVE = "01ff1800040000000000010000000000000000000000"
