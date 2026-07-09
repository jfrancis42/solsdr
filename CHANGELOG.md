# Changelog

All notable changes to solsdr. This project is **alpha**; the SunSDR2 **PRO**
is hardware-verified, the **DX** is not.

## Unreleased

- **Panadapter** (`clients/panadapter.py`): standalone live spectrum + waterfall
  display — PyQt (5/6) / PySide6 + pyqtgraph + numpy, no GNU Radio, no
  ExpertSDR3. Display-only (never tunes/keys). Shared absolute-frequency axis,
  auto/fixed scaling, dBFS (or dBm via `--ref-offset`), mouse crosshair readout,
  perceptual colormaps, averaging/peak-hold/DC-hide, adjustable FFT + window,
  draggable spectrum/waterfall split, and a live info bar (freq/mode/PTT/power/
  S-meter/span/RBW) driven off the control API. Includes a `--file` mode to
  replay a recorded capture with no radio (loops for a hands-off demo).
  Performance-tuned for CPU-only boxes (30 fps+): auto-scale re-ranges on a
  timer (`--rescale`, default 5 s; `R` snaps now) instead of every frame, and
  the trace is a thin non-antialiased line by default — the two big software-
  render costs. `--pretty` restores a filled antialiased trace for GPU/fast hosts.
- **RX IQ server is now ON BY DEFAULT** in `solsdr_receiver.py` (port 5555) — the
  panadapter, GNU Radio, and recorders can attach with no flag. New
  `--no-iq-server` disables it; `--iq-server` is kept as a no-op for
  back-compatibility. The TX IQ server stays opt-in (`--iq-tx-server`) — transmit
  remains a deliberate act.

## 0.2.0

- **RX preamp/attenuator** control (`0x05` states `0x80`–`0x83`): `set_preamp()`,
  control-API `preamp`, shell `preamp <state>`. dB labels unverified on hardware.
- **RIT** (receiver incremental tuning): baseband IQ shift, control-API `rit`,
  shell `rit <hz>`.
- **S-meter** exposed on the text control API (`smeter` command + `smeter=` in
  `status`). Note: a real CAT S-meter (into JS8Call/WSJT-X) is **not** available —
  the Hamlib dummy backend generates its own `STRENGTH` and rejects writes.
- **Clean power-off**: `Radio.close(power_off=True)` sends `0x02`.
- **Real logging** (`solsdr/log.py`): levels/timestamps; `--log-level`.
- **Config file**: `~/.config/solsdr/config.*` (JSON or flat `key = value`);
  CLI overrides config overrides built-in defaults; `--config <path>`.
- **Frequency-range validation** against the radio profile.
- **`--version`** flag; version unified at 0.2.0.
- **Raw-IQ TX server** (`--iq-tx-server`, :5558): the transmit counterpart of
  `--iq-server`. A TCP client sends raw `complex64` baseband IQ at the wire rate
  and the radio transmits it verbatim (gain + clip, no modulation/resample) —
  GNU Radio → radio. `TXSession.enter_tx(iq_input=True)` feeds the shared IQ
  buffer directly; connect keys, disconnect unkeys; one transmitter at a time.
  Disarmed by default (no RF) — `--tx-arm` keys. Obeys all TX interlocks.
  **Hardware-verified 2026-07-08** into a dummy load: fed complex IQ over TCP,
  fwd_power rose (0 → ~29 raw at 3 W on 20 m) with a ~1.5 A DC current rise.
- **Network control** now reaches squelch/AGC/NR/preamp/RIT via the text API.
- **Packaging**: `pip install .` installs the `solsdr` command; `solsdr.audio`
  bridge package now included in the wheel. systemd unit templates in `systemd/`.
- **RX2** second receiver (interleaved on port 50002, per-receiver IQ servers);
  phase-coherent (γ²≈0.999) on a shared antenna. **TX-vs-RX2 resolved
  2026-07-08:** neither receiver streams during a key-down — the single 50002
  link carries 0xFD TX frames instead of RX IQ (both RX1 and RX2 drop to <1 % of
  their packet rate while keyed). RX2 is for dual-*watch*, not receive-through-TX.
- **Out-of-band TX confirmed 2026-07-08:** the PRO keys and makes RF off the ham
  bands (verified at 13000 kHz into a dummy load) — no firmware band lock. Note
  fwd_power reads lower off-band for the same DC draw (no band-specific match).

## 0.1.0

- Initial: PRO wake/power-on/tune/stream, 24-bit Q-first IQ codec, USB/LSB/AM/
  FM/CW demod, TX (audio→IQ→paced UDP) with safety interlocks, text control API,
  raw IQ TCP server, JS8Call/WSJT-X audio bridge via real rigctld.
