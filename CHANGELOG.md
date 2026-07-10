# Changelog

All notable changes to solsdr. Maturity varies: **RX1 + TX (CW/digital/voice)
are beta** on the SunSDR2 **PRO** (hardware-verified); **RX2 is alpha**; the
**DX** variant is unverified.

## Unreleased

- **Voice / SSB is now a first‑class TX source**, hardware‑verified on‑air (PRO,
  into a dummy load). Three sources via `tx source pc|mic1|mic2`:
  - **Front‑panel Mic1/Mic2** — the radio digitizes the mic and streams it back
    to the host as downstream `0xFD` frames (mono in the IQ wrapper); solsdr
    modulates it and streams TX IQ up. New `packet.decode_tx_audio_packet()`,
    radio `on_tx_audio` callback + RX‑loop dispatch, wire‑rate mic path. See
    ARTEMISSDR.md §7.
  - **PC / USB mic** — audio on the `solsdr-tx` sink (route a mic in with
    `pactl load-module module-loopback source=<mic> sink=solsdr-tx`).
- **Hardware / external PTT** (`tx hwptt on`): the radio's external‑PTT input
  (footswitch, hand‑mic PTT) is decoded from its `0x1F` edge packet
  (`packet.parse_ptt_edge()`, radio `on_ptt_edge` callback) and keys/unkeys TX
  alongside CAT PTT. Telemetry parsing now rejects that packet (byte‑2 subtype).
  See ARTEMISSDR.md §8.
- **Software / spacebar PTT.** `key`/`unkey` (aliases `ptt`/`unptt`) are explicit
  software PTT; `voice` is a push‑to‑talk console — **hold SPACE** to talk, **tap
  ENTER** to toggle a latched hands‑free over, `q`/`Esc` exits.
- **Per‑source mic calibration + comms voice shaping.** `tx cal [s]` measures
  your normal speaking level and computes a fixed input gain (mic1/mic2 key the
  radio to measure — dummy load; pc measures the sink with no RF). `tx shape
  [src] flat|comms|dx` selects a voice band‑pass + speech‑compression preset
  (SSB is comms, not hi‑fi). `tx gain [src] <x>` sets a gain by hand. Saved per
  source in config (`tx_src_<src>_{gain,shape,cal}`) and re‑applied on source
  switch. The `Modulator` gained shaping presets + a fixed‑gain (leveling‑off)
  path; a calibrated source uses fixed gain like a real rig, uncalibrated keeps
  auto‑leveling.
- **Adjustable voice→RF latency.** `tx latency <ms>` sets the IQ pre‑buffer ahead
  of the pacer (the dominant voice→RF delay); default lowered from ~800 ms to
  **120 ms**, live‑tunable and saved as `tx_latency_ms`.
- **Startup rigctld reaper.** `RigctldPoller.start()` now kills any orphaned
  `rigctld` already bound to its CAT port before launching its own — a leftover
  from a crashed/killed solsdr no longer silently shadows the port and breaks
  CAT (it had made JS8Call unable to change frequency).
- **Control API `status`** now includes `fwd_power_raw`/`volts`/`amps` from
  telemetry, so a client can confirm TX remotely.
- **PipeWire RX output pinning** documented: set `PULSE_SINK=<sink-name>` to keep
  RX audio on a fixed output when USB mics/headsets hotplug and steal the default
  sink.

- **Text control API (:5556) is now ON BY DEFAULT** (loopback), matching
  `--iq-server` — the panadapter and other clients read it for live
  freq/mode/S-meter and to send commands. `--no-control-api` disables it.
- **Virtual audio device naming from the shell.** `tx prefix <name>` renames the
  bridge's PulseAudio devices live → `<name>-rx.monitor` (fldigi/WSJT-X **input**)
  and `<name>-tx` (**output**); `tx` now shows the exact device names. Still
  settable at launch with `--prefix`. Live rename drops apps bound to the old
  names and is refused while transmitting.

- **Renamed the main script** `solsdr_receiver.py` → `solsdr/cli.py` (it's a
  transceiver now, not a receiver). Run it as **`solsdr`** (installed; alias
  `solsdr-shell`) or **`python3 -m solsdr`** (from source). The old
  `solsdr-receiver` console script is gone.
- **CW keyboard sending, with Farnsworth.** `cw <text>` in the shell transmits
  the text as Morse via the existing `CWEncoder` → an interlocked `TXSession`.
  `tx wpm <char> [<word>]` sets element/Farnsworth spacing speed (e.g.
  `tx wpm 25 15` = 15 wpm effective at 25 wpm element speed); `tx cwtone <Hz>`
  sets the sidetone (default 600). `cw on|off|pitch|bw` keep their RX meanings.
- **Unified transceiver: `solsdr` is now one program for RX and TX.** The
  digital-mode/TX bridge (virtual audio + real rigctld + PTT→TXSession) runs
  **in-process by default**, sharing the single `Radio` via IQ fan-out (the
  receiver owns `start_stream` and feeds the bridge with `feed_iq`; no second
  process, no config-relay). The interactive shell now controls TX **live**:
    - `tx` — show all TX settings; `tx power <W>`, `tx maxpower <W>`,
      `tx mode <m>`, `tx micgain <x>`. Power and mic gain apply to an
      **in-progress transmission** immediately; max-power (amp-protection
      ceiling) only takes effect on the next over and is never raised live.
    - `tune [seconds] [watts]` — the ONE shell command that keys the radio: a
      deliberate, time-bounded CW tuning carrier (default 3 s, current power),
      via the interlocked TXSession (arm, amp-limit, calibration gate, dead-man);
      refuses if a transmission is already in progress. Everything else is
      app/CAT-driven PTT.
    - `read-config` / `write-config` — apply the config file to the live radio,
      or snapshot all current live parameters into it.
    - `devices` — list audio devices (sounddevice + PulseAudio sinks).
    - `help`/`?`, plus `agc`/`gain`/`vol` for RX audio level, and `cw pitch|bw`.
  `--no-tx` reverts to RX-only; TX still needs PulseAudio + Hamlib rigctld (warns
  and continues RX-only if absent). `config.py` gained `update()`/`config_path()`.

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
  Frequency **zoom** via `+`/`−`/`Full` toolbar buttons (and `+`/`-`/`0` keys),
  centered on the tuned frequency; spectrum and waterfall stay aligned.
- **RX IQ server is now ON BY DEFAULT** in the transceiver shell (port 5555) — the
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
