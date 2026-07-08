# solsdr

**A pure-Python software-defined radio for the Expert Electronics SunSDR2 PRO — no ExpertSDR3 required.**

`solsdr` is a lightweight replacement for ExpertSDR2/3 that talks directly to a
SunSDR2 PRO over the network. It wakes and powers on the radio, tunes it, streams
IQ, demodulates to audio, and (with the transmit chain) modulates audio back to
IQ. It's built for **experimentation**:

- **GNU Radio** — consume the radio as raw complex64 IQ over TCP, *or* as
  demodulated audio, and drive it from your own flowgraphs.
- **Digital modes** — run **JS8Call**, **WSJT-X**, or **fldigi** against the radio
  through virtual audio devices and a Hamlib-compatible control port, with no
  vendor software in the loop.
- **Scripting / DSP** — a clean Python API (`Radio`, `Demodulator`, `Modulator`,
  `TXSession`) for building your own receivers, transmitters, and measurements.

**A core design goal: solsdr has no GUI, by intent.** The entire SDR — every bit
of its functionality — is a "front‑panel‑less" command‑line program, meant to be
*remote‑controlled by other software* rather than operated by hand. There is no
waterfall, no spectrum display, no knobs, dials, meters, or switches, and there
never will be. Everything is driven over the network: Hamlib (`rigctld`),
JS8Call, WSJT‑X, fldigi, GNU Radio, or your own scripts. If you want a
point‑and‑click SDR console, this isn't it (use ExpertSDR3); solsdr is the
headless engine that lets *other* tools use the radio without any of that.

Author: **Jeff Francis, N0GQ**.

> ### ⚠️ Alpha release
>
> This is essentially an **alpha**. The core RX and TX paths work and are
> hardware‑verified, but the project is young: expect rough edges, incomplete
> coverage (VHF, the DX variant, antenna switching), and gaps in the docs.
> Further **testing, refinement, enhancement, and documentation are
> forthcoming.** Use it in that spirit — and always validate TX into a dummy
> load before going on the air.

> ### Model support
>
> **The SunSDR2 PRO is hardware‑verified. A SunSDR2 DX profile is fully
> implemented but UNVERIFIED.** The DX differs from the PRO in several critical
> details (magic byte `0x32`, 312.5 kHz native rate, 92.5 kHz DDC offset, RX+TX
> bidirectional on a single port `50002`, and a distinct power‑on sequence). All
> of these values are populated in `solsdr/protocol/profiles.py` from the
> ArtemisSDR reference — select the DX with `--variant DX` — but **this project
> has never run against a real DX.** The code prints an "UNVERIFIED" warning when
> a DX profile is used, and may not work. DX owners: please try it and report
> results so the profile can be confirmed (or corrected).

---

## Credits — protocol reverse engineering

The SunSDR2 network protocol is **not publicly documented by the manufacturer.**
Everything this project does is possible only because of the reverse‑engineering
work in **[ArtemisSDR](https://github.com/kk68/ArtemisSDR) by K0KOZ**, whose
`ChannelMaster/sunsdr.c` implementation was the reference for the discovery,
power‑on, control, tuning, and IQ‑framing formats. Full credit and thanks to
K0KOZ and the ArtemisSDR project — without their work, none of this would exist.

A note on the relationship between the two projects, offered in the same spirit
of sharing findings: the ArtemisSDR authors state they had only a SunSDR2 **DX**
to test with, so several of their PRO‑specific constants are extrapolated from
the DX. Where this project found the PRO to differ (verified on real PRO
hardware), it uses corrected values, and it has decoded some SunSDR2 features
ArtemisSDR doesn't implement. **Those PRO findings — corrected constants, new
opcodes, and a likely fix for ArtemisSDR issue #47 — are collected in
[`ARTEMISSDR.md`](ARTEMISSDR.md)**, contributed back to that project.

---

## Status

**Receive: working and hardware‑verified.** Live FT8 decoded off‑air (validated
with WSJT‑X's `jt9`), WWV AM confirmed, CW demod/decode validated end‑to‑end,
and live JS8Call decoding through the virtual‑audio bridge.

**Transmit: working — real RF verified on all HF bands.** The complete
audio→IQ→paced‑wire chain, PTT/drive/PA control, timing, and safety interlocks
are implemented, calibrated into a dummy load (6–17 W across HF), and driven
on‑air by JS8Call through the audio bridge. See **Transmit** below.

> **HF focus:** development and testing so far have concentrated on **HF**.
> The PRO's VHF paths (2 m, the VHF LNA, VHF‑specific antenna/TX routing) are
> largely untested here and some code carries HF‑only assumptions. VHF was
> deliberately never keyed during TX calibration (no load on that port). Treat
> VHF operation as unverified for now.

| Capability | State |
|---|---|
| Broadcast wake / discovery | ✅ verified (byte‑identical to ExpertSDR3's probe) |
| Power‑on (verified PRO init sequence) | ✅ verified |
| Tuning, live retune (no restart) | ✅ verified |
| RX IQ streaming @ 39062.5 Hz + bidirectional keepalive | ✅ verified |
| Selectable IQ rate 39062.5 / 78125 / 156250 / 312500 Hz | ✅ verified (FT8 decoded at 312.5 kHz) |
| **RX2 second receiver** (independent freq/mode; dual-watch) | ✅ verified — interleaved on one stream, per-receiver demod/IQ; `--rx2 <kHz>` |
| RX1↔RX2 phase coherence (shared antenna) | ✅ measured **γ²≈0.999** — coherent dual-channel (DF/beamforming viable; see below) |
| External 10 MHz reference (GPSDO) on/off | ✅ opcode verified (bytes identical to ExpertSDR3) |
| Supply telemetry: voltage / current / temperature / forward‑power | ✅ `0x1F` fully decoded; shown in status (V/A, °F). Includes a forward‑power field (`fwd_power_raw`). No SWR is reported by the radio. Temperature is read‑only — the radio runs its own fan in firmware (no host setpoint) |
| Front-end: HF.LPF, VHF.LNA toggles | ✅ opcodes verified (relay‑confirmed); `lpf`/`lna` shell cmds |
| Mic source (Mic1/Mic2/PC) | ✅ verified (`0x21`); `mic` shell cmd. Mic gain is client‑side (no radio cmd) |
| USB / LSB / AM / FM / CW demodulation + S‑meter | ✅ verified |
| CW receive: BFO demod + Morse decoder | ✅ verified (synthetic full‑chain) |
| Automatic reconnection on network loss | ✅ verified (simulated interruption) |
| CAT control via real Hamlib `rigctld` (mirrored to the radio) | ✅ verified with JS8Call/WSJT‑X/`rigctl` |
| Raw‑IQ TCP server + text control API (GNU Radio client) | ✅ verified |
| **Raw‑IQ TX server** (GNU Radio → radio; complex64 in, transmitted verbatim) | ✅ **hardware‑verified** (2026‑07‑08, RF into dummy load) — `--iq-tx-server` (:5558); disarmed by default, `--tx-arm` to key |
| Virtual‑audio bridge for JS8Call / WSJT‑X / fldigi | ✅ RX decoding + TX on‑air verified |
| Stateful DSP filters (NR / NB / notch / APF / squelch) | ✅ unit‑tested |
| Transmit chain (SSB/AM/FM modulate, pacing, PTT/drive/PA) | ✅ on‑air verified into dummy load |
| Per‑band TX power calibration + amp‑protection limit | ✅ calibrated on all 10 HF bands (wattmeter‑anchored); ~6 W (160 m) to ~17 W (10 m) |
| Antenna port selection | ⛔ not implemented — PRO opcode not yet identified |
| SunSDR2 DX support | ⚠️ fully coded from ArtemisSDR, **untested on hardware** (`--variant DX`) |
| CW transmit | ⛔ not built (encoder exists) |

### Known gaps / TODO

- **Audio output routing** — the mic *source* selector (`0x21`) is decoded, but
  the radio's front‑panel audio *output* routing (e.g. Phones on the front) has
  not been captured or identified. Needs an EESDR3 capture of the output‑routing
  controls.
- **GPSDO lock/sync status** — the external‑reference *select* bit (`0x1D`) is
  decoded, but whether the radio reports 10 MHz *lock* isn't known. Capture the
  radio with the reference present vs. absent and diff for a status bit/packet.
- **Antenna port selection** — PRO opcode not yet identified (ArtemisSDR's `0x15`
  mapping doesn't apply to the PRO).
- **TX frequency coverage** — RESOLVED (2026‑07‑08): the PRO **transmits out of
  band**. Keyed at 13000 kHz (non‑ham) into a dummy load, it made RF with no
  firmware band lock and no tune refusal. Note off‑band forward power reads lower
  for the same DC draw (no band‑specific output match), and no band there is
  calibrated — so use raw drive, not a watts setpoint, off the ham bands. TX
  responsibly and legally.
- **VHF** — largely untested; deliberately never keyed during TX calibration.
- **SunSDR2 DX** — profile is coded from ArtemisSDR but unverified on hardware.
- **CW transmit** — not built (the encoder exists).

---

## Install

```bash
# Option A — install the package (gives you the `solsdr` command):
pip install .                          # from a clone; or:  pip install solsdr

# Option B — run from a source checkout without installing:
pip install -r requirements.txt        # numpy, scipy, sounddevice
```

`rigctld` (from `hamlib` / `libhamlib-utils`) is required for CAT control and
the digital-mode bridge. For an always-on headless setup, see
[`systemd/README.md`](systemd/README.md).

## Quick start — receive

```bash
# Receive 20 m FT8 with live audio; type commands at the sdr> prompt.
solsdr 14074 --device 5                # if installed
python3 solsdr_receiver.py 14074 --device 5   # from a source checkout
```

`--device N` selects the audio output (list with
`python3 -c "import sounddevice as sd; print(sd.query_devices())"`). On a
PipeWire/PulseAudio desktop use the `pipewire`/`pulse` device, **not** a raw
`hw:` ALSA device.

---

## Configuration file

Rather than retype `--radio-ip`, `--local-ip`, `--device`, etc. on every run,
put station defaults in `~/.config/solsdr/config.*` (searched in this order:
`config.json`, `config.conf`, `config.ini`, `config.cfg`). Keys mirror the CLI
flag names (argparse `dest`), so `--local-ip` → `local_ip`, `--radio-ip` →
`radio_ip`, and so on. **Precedence: CLI flag > config file > built‑in default**,
so a flag always wins over the file.

Flat `key = value` format (`config.conf`) — values are auto‑typed (int/float/
bool); `#` starts a comment:

```ini
# ~/.config/solsdr/config.conf
radio_ip  = 10.1.2.3
local_ip  = 10.1.2.185
device    = 5
variant   = PRO
freq_khz  = 14074
mode      = USB
rate      = 39062.5
ext_ref   = true
log_level = info
```

Or JSON (`config.json`) if you prefer:

```json
{
  "radio_ip": "10.1.2.3",
  "local_ip": "10.1.2.185",
  "device": 5,
  "freq_khz": 14074,
  "mode": "USB",
  "ext_ref": true
}
```

With that in place, `solsdr` (no args) tunes 20 m USB on the right radio; add a
frequency to override just that: `solsdr 7074`. Point at a different file with
`--config /etc/solsdr/config.conf` (handy for the systemd service). Unknown keys
are ignored with a warning, so a shared config can carry extra keys.

---

## Interactive shell — commands with examples

While the receiver runs, type at the `sdr>` prompt. Bare commands act on RX1;
prefix with `2 ` to target the second receiver (see [RX2](#second-receiver-rx2--dual-watch)).

| Command | Example | Effect |
|---|---|---|
| `<kHz>` | `7074` | Tune to 7074 kHz |
| `m <mode>` | `m CW` | Set mode: USB / LSB / AM / FM / CW / CWU / CWL |
| `cw on\|off` | `cw on` | Toggle the live Morse decoder |
| `s` | `s` | Print S‑meter + full status line |
| `ref ext\|int` | `ref ext` | External 10 MHz (GPSDO) vs internal reference |
| `lpf on\|off` | `lpf on` | HF low‑pass filter relay |
| `lna on\|off` | `lna on` | VHF LNA |
| `preamp <state>` | `preamp -10` | RX preamp/att: `-20` `-10` `0` `+10` dB, or `off`/`preamp` |
| `mic <src>` | `mic pc` | Mic source: `mic1` / `mic2` / `pc` |
| `rit <Hz>` | `rit 250` | Receiver incremental tuning (`rit 0` = off) |
| `nr <0-1>` | `nr 0.4` | Noise reduction strength |
| `nb <0-1>` | `nb 0.3` | Noise blanker |
| `notch <Hz>` | `notch 800` | Manual notch (`notch 0` = off) |
| `apf <0-1>` | `apf 0.6` | Audio peak filter (CW) |
| `sql <0-1>` | `sql 0.2` | Squelch threshold |
| `2 <cmd>` | `2 m CW` | Run any of the above against RX2 (e.g. `2 7074`) |
| `q` | `q` | Quit |

---

## CLI flags — common use cases

```bash
# Plain listening: 40 m LSB, audio out on device 5
solsdr 7185 --mode LSB --device 5

# Point at a specific radio / interface (overrides the config file)
solsdr 14074 --radio-ip 10.1.2.3 --local-ip 10.1.2.185

# Expose CAT to WSJT-X / fldigi / JS8Call (real rigctld on :4532)
solsdr 14074 --hamlib

# Stream raw IQ to GNU Radio (complex64 on tcp :5555)
solsdr 14074 --iq-server

# Accept raw IQ from GNU Radio and TRANSMIT it (tcp :5558). No RF without --tx-arm.
solsdr 14074 --iq-tx-server                        # wiring test, no RF
solsdr 14074 --iq-tx-server --tx-arm --max-power-watts 5 --tx-watts 3

# Everything at once: CAT + IQ + text control API, wider 312.5 kHz IQ
solsdr 14074 --hamlib --iq-server --control-api --rate 312500

# Dual-watch: RX1 20 m (audio + IQ :5555), RX2 40 m CW (IQ :5557)
solsdr 14074 --rx2 7025 --rx2-mode CW --iq-server

# External 10 MHz reference (GPSDO) on / off
solsdr 14074 --ext-ref
solsdr 14074 --no-ext-ref

# Headless for a fixed time then exit (no prompt) — scripting/capture
solsdr 14074 --iq-server --seconds 60

# Move the CAT or IQ ports
solsdr 14074 --hamlib --hamlib-port 4540 --iq-server --iq-port 5560

# SunSDR2 DX (UNVERIFIED profile) and quieter logging
solsdr 14074 --variant DX --log-level warning

# Show version
solsdr --version
```

`--iq-port` sets RX1's IQ port; RX2's is always RX1+2 (default 5557). Full list:
`solsdr --help`.

---

## GNU Radio — IQ in and out

solsdr exposes the radio to GNU Radio (or any SDR tooling) two ways: **raw IQ
over TCP** for receive, and **audio into the digital‑mode bridge** for transmit.

### Receiving IQ (radio → GNU Radio)

Start the IQ server and connect a **TCP source**:

```bash
solsdr 14074 --iq-server --rate 312500        # complex64 IQ on tcp 0.0.0.0:5555
```

On the first connect, solsdr sends one newline‑terminated text header, then a
continuous stream of interleaved little‑endian `float32` I/Q (i.e. GNU Radio
`complex float 32`):

```
SOLSDR IQ rate=312500.0 fmt=complex64 freq=14074000\n<raw complex64 samples…>
```

In GNU Radio Companion:

1. **Socket PDU** or **TCP Source** — use a *TCP Client* **Socket PDU**, or the
   `blocks.socket_pdu`/a TCP source block, pointed at `<host>:5555`,
   Type = **Complex Float 32**.
2. Set the flowgraph **sample rate** to match `--rate` (39062.5 / 78125 / 156250
   / 312500). solsdr announces it in the header line; read it once and hardcode
   the variable, or strip the header in a small Python block.
3. The IQ is baseband, centered on the tuned frequency; set your **QT GUI Sink**
   center frequency to the tuned freq so the axis reads in absolute Hz. A ready
   combined FFT+waterfall flowgraph is in
   [`clients/gnuradio/qt_iq_waterfall.py`](clients/gnuradio); see
   [`clients/README.md`](clients/README.md).

The header ends at the first `\n`; a client that doesn't care can skip those
bytes and treat the rest as pure `complex64`. RX2, if enabled, is a second
identical stream on **:5557**, so a flowgraph can pull both receivers for
coherent two‑channel work (see [phase coherence](#second-receiver-rx2--dual-watch)).

### Transmitting IQ from GNU Radio (GNU Radio → radio)

The **raw‑IQ TX server** is the transmit counterpart of `--iq-server`: it accepts
raw `complex64` baseband IQ over TCP and streams it to the radio verbatim (gain +
clip only — no modulation, no resampling), so your flowgraph *is* the modulator.

```bash
# Raw-IQ TX. Off (no RF) unless you add --tx-arm. Always into a dummy load first.
solsdr 14074 --iq-tx-server                       # wiring test: runs, no RF
solsdr 14074 --iq-tx-server --tx-arm \
       --max-power-watts 5 --tx-watts 3           # ARMED: keys on connect
```

Then in GNU Radio Companion, end your TX chain in a **TCP Sink** (or
`blocks.socket_pdu` as a TCP client), Type = **Complex Float 32**, pointed at
`<host>:5558`:

1. Produce **baseband IQ at the radio wire rate** (39062.5 Hz by default — match
   `--rate`). There is no resampler on this path, so a rate mismatch transmits at
   the wrong speed; the server announces the required rate in its
   `SOLSDR IQTX rate=… fmt=complex64` header. Keep samples in `[-1, 1]`
   (the server clips at ~0.98 to protect the 24‑bit packing).
2. **The radio keys automatically while a client is connected** and unkeys on
   disconnect (or after an idle gap). Only one transmitter at a time. No separate
   PTT step is needed — connecting *is* keying.

So a full baseband‑to‑RF SSB/data waveform you build in GNU Radio goes straight
out the antenna, and RX (`--iq-server`, :5555) + TX (`--iq-tx-server`, :5558) give
you a symmetric complex‑IQ pipe in and out of the radio.

**Alternative — audio bridge:** for JS8Call/WSJT‑X/fldigi (or any app that emits
*audio*, not IQ), use the bridge instead, which runs the audio through solsdr's
own SSB/AM/FM modulator and keys via CAT:

```bash
python3 -m solsdr.audio --radio 10.1.2.3 --local-ip 10.1.2.185 \
    --freq 14074000 --tx-mode USB --max-power-watts 5 --tx-watts 3
# app TX audio -> solsdr-tx sink -> modulator -> radio;  PTT = CAT
```

⚠️ Both TX paths obey the same safety interlocks (arming, amp‑protection watt
ceiling, calibration gating, dead‑man). The raw‑IQ server is **disarmed by
default** — it runs the whole chain with no RF until you pass `--tx-arm`.
**Always key into a dummy load first.** For pure IQ *analysis* GNU Radio only
needs the RX path above.

### Second receiver (RX2 / dual-watch)

The PRO's second receiver runs alongside the first with an independent frequency
and mode — e.g. watch 20 m and 40 m at once:

```bash
python3 solsdr_receiver.py 14074 --rx2 7074 --iq-server
#   RX1 -> audio + IQ on :5555   |   RX2 -> IQ on :5557
#   --rx2-mode CW / --rx2-device N  set RX2's mode / audio output
```

Both receivers stream interleaved on one link (the radio tags each packet with
its receiver index); solsdr routes them to independent demodulators and per-
receiver IQ servers (RX1 :5555, RX2 :5557). In the interactive shell, prefix a
command with `2 ` to target RX2 (e.g. `2 7074`, `2 m CW`); bare commands act on
RX1. Both receivers share one sample rate (a hardware constraint).

**RX2 is for dual-*watch*, not receive-through-transmit.** Verified 2026-07-08:
while RX1 is keyed, **neither** receiver streams — the single UDP link on port
50002 carries the `0xFD` TX frames in place of RX IQ, so both RX1 and RX2 packet
rates drop to under 1 % of normal for the key-down and resume the instant you
unkey. Fine for monitoring two frequencies between overs; not a way to hear RX2
*during* a transmission.

**Phase coherence:** fed from a single antenna, the two receivers are strongly
phase-coherent — measured **γ² ≈ 0.999** at the signal (`tools/rx2_coherence.py`).
That makes coherent dual-channel work (direction finding, beamforming, two-
antenna noise cancelling) viable, with two caveats: the fixed phase offset is
**not** repeatable across restarts (needs a per-session phase calibration), and
real DF needs **two separate antennas** (the measured coherence is what makes the
inter-antenna phase difference meaningful once you split the feed).

---

## JS8Call / WSJT‑X / fldigi (audio + control, no ExpertSDR3)

The audio bridge presents the radio to digital‑mode software as **virtual
PulseAudio devices** (RX audio in, TX audio out) plus a **real Hamlib rigctld**
for CAT and PTT — so the app runs exactly as it would with any radio, but with
nothing between it and the SunSDR2 except this project.

```bash
# Bring up virtual audio + CAT, tuned to 20 m JS8, TX capped at 5 W / set to 3 W
python3 -m solsdr.audio --radio 10.1.2.3 --local-ip 10.1.2.185 \
    --freq 14078000 --tx-mode USB \
    --max-power-watts 5 --tx-watts 3
```

Then in JS8Call (or WSJT‑X / fldigi):

- **Rig:** `Hamlib NET rigctl`, server `127.0.0.1:4532`, **PTT = CAT**
- **Audio input (RX):** `solsdr-rx.monitor` (or `solsdr-rx-mic` if the app hides
  monitor sources)
- **Audio output (TX):** `solsdr-tx`

Control goes to a genuine `rigctld` (the bridge launches it and mirrors
freq/mode/PTT to the radio), so CAT/PTT/split negotiation is Hamlib's own
battle‑tested code rather than a reimplementation.

Run `python3 -m solsdr.audio --help` for all flags. Key ones: `--max-power-watts`
(runtime‑locked amp‑protection ceiling), `--tx-watts` (output setpoint),
`--prefix` (device name prefix), `--monitor <sink>` (also play RX+TX audio to a
speaker so you can hear glitches). If the app can't open the device, quit it,
start the bridge first, then relaunch the app so it binds to the current
PulseAudio nodes.

---

## Radio control (CAT) — Hamlib `rigctld` is required

solsdr does **not** implement the rigctld wire protocol itself. Control software
(WSJT‑X, fldigi, JS8Call, Log4OM, `rigctl`, …) talks to a **real Hamlib
`rigctld`**, and solsdr connects to that same rigctld as a second client,
mirroring its frequency/mode/PTT to the radio over UDP. This means all the
finicky CAT/PTT/split capability negotiation is Hamlib's own battle‑tested code,
not a reimplementation — the tradeoff is that **`rigctld` must be installed**
(Debian/Ubuntu: `libhamlib-utils`; Arch: `hamlib`).

Both `solsdr_receiver.py --hamlib` and `python3 -m solsdr.audio` launch the
rigctld for you (Hamlib dummy backend, model 1) on port 4532 and do the
mirroring. You do not start rigctld yourself; you just point your software at it:

```
Your software (WSJT-X/fldigi/JS8Call/Log4OM/rigctl)
        │  Hamlib NET rigctl  ->  127.0.0.1:4532
        ▼
   real rigctld (dummy backend)   ← launched by solsdr
        ▲
        │  solsdr polls freq/mode/PTT and mirrors to the radio (UDP)
   SunSDR2 PRO
```

**Client configuration:**

- **WSJT‑X / JS8Call / fldigi:** Rig = `Hamlib NET rigctl`, Network Server =
  `127.0.0.1:4532`. For TX, set PTT method = `CAT`.
- **`rigctl` (command line):** talk to the running rigctld as a NET client —
  ```bash
  rigctl -m 2 -r 127.0.0.1:4532        # -m 2 = "NET rigctl"
  # then, at the prompt:  F 14074000   (set freq)   f  (get freq)
  #                       M USB 3000   (set mode)   m  (get mode)
  ```
- **Any Hamlib app:** point its rigctld/NET‑rigctl host at `127.0.0.1:4532`.

If you need rigctld on a different port, use `--hamlib-port N` (receiver) or
`--hamlib-port N` (`solsdr.audio`).

---

## How it works — verified PRO protocol facts

Confirmed against real PRO hardware. Where noted, they differ from the ArtemisSDR
reference (whose PRO values were extrapolated from a DX):

| Fact | Value |
|------|-------|
| Control | UDP port 50001. The client **must bind source port 50001** — the radio ignores control traffic from any other source port (discovery still works from an ephemeral port). |
| Wake / discovery | Broadcast probe `<family> ff 00 1a` + one's‑complement checksum to `<subnet>.255:50001` and `255.255.255.255:50001`. The radio replies from any powered state — no power‑cycle needed. |
| Magic byte | PRO `0x01` (DX `0x32`). |
| RX IQ | UDP 50002, 1210‑byte packets = 10‑byte header + 200 complex samples, 24‑bit **Q‑first** little‑endian. |
| **PRO IQ rate** | Selectable **39062.5 / 78125 / 156250 / 312500 Hz** (index 0–3). Default 39062.5 (195 pkt/s). Set by a rate *index* in the STATE_SYNC packet — see `ARTEMISSDR.md`. |
| **RX keepalive** | The PRO RX stream is **bidirectional on 50002**: the client must echo one silence packet per received packet, or streaming stops after ~8 s. |
| Tuning | `0x09` primary + `0x08` companion. **PRO DDC offset = 0** (the ArtemisSDR 92.5 kHz value is DX‑specific and yielded only noise on the PRO). |
| **TX IQ** | UDP **50002** (same port as RX — verified; *not* 50003), opcode `0xFD`, paced every **5.12 ms** (< 1 ms jitter required). |
| TX control | PTT `0x06`, drive `0x17` (raw 0–255 byte), PA `0x24`. TX entry order: config‑block(TX) → drive → MOX. Drive→watts is per‑band (calibrated, see below). |
| Reference clock | `0x1D` u32: `1` = external 10 MHz (GPSDO), `0` = internal. The PRO boots with external enabled. |

Higher sample rates cost proportionally more CPU/network (312.5 kHz is 8× the
data of 39062.5); fine on a workstation or Pi 4/5, tighter on a Pi 3/Zero.

> **Reverse‑engineering notes** — the wire‑level details (sample‑rate index
> mechanism, `0x1D`/`0x1F` opcodes, front‑end toggles, PRO‑vs‑DX differences, and
> the ArtemisSDR issue #47 fix) live in [`ARTEMISSDR.md`](ARTEMISSDR.md),
> contributed back to that project. Most users don't need it.

---

## Transmit (working — RF verified on all HF bands)

The full transmit path is implemented, validated to a loopback socket with real
program audio (encode → SSB‑modulate → packetize → pace at 5.12 ms → demodulate
back → speech‑envelope match), **and confirmed producing real RF into a dummy
load across the whole HF range**, including on‑air JS8Call transmissions.

- **Real‑time modulator** (`dsp/modulator.py`): USB/LSB/AM/FM, Hilbert‑based SSB,
  with input leveling so a quiet app (e.g. JS8Call with its slider well down)
  still drives the modulator to full scale — TX power depends on the drive byte,
  not the app's volume.
- **Precise pacer** (`protocol/tx_pacer.py`): Linux `timerfd` + `SCHED_FIFO`.
  Measured jitter well under the 1 ms budget (≈0.03–0.7 ms), even under load.
- **Orchestration** (`tx_session.py`): the exact ExpertSDR3 TX entry/exit
  command ordering.

### Safety interlocks (all enforced in code)

- **Arming:** nothing keys the radio unless `arm(confirm=True)` is called;
  unarmed runs the whole chain to a loopback with no RF.
- **Amp‑protection power limit:** an output‑watts ceiling (e.g. 5 W to protect a
  downstream amplifier) is set **only** at construction from CLI/config — it has
  no runtime setter, so neither the interactive shell nor a Hamlib client can
  raise it. It clamps both watts and raw‑drive requests.
- **Calibration‑gated:** because the watts→drive mapping is only trustworthy on
  a calibrated band, the amp limit **refuses to key on an uncalibrated band**.
- **Dead‑man auto‑unkey** (default 5 min, refreshed by live audio flow) and a raw
  drive ceiling.

⚠️ **Always validate your own TX chain into a dummy load + wattmeter (and ideally
a spectrum analyzer) before going on a real antenna.**

### Per‑band power calibration — you must do your own

Absolute TX watts depend on your bench (any RF‑sample tap/attenuator, coax, and
the individual radio), so **the calibration is per‑installation — every user must
run it.** The tooling is included:

- `tools/tx_firstkey.py` — safe first key‑up (steady tone into a dummy load).
- `tools/tx_anchor.py` — anchor one band's output to a through‑line wattmeter
  reading (a ~20 s keydown).
- `tools/tx_bandcal.py` — sweep drive across a band and build the drive→watts
  curve; results load into `TXPowerCal` and are stored in
  `~/.config/solsdr/tx_power_cal.json`.
- `tools/cal_tap.py` — characterize an RF‑sample tap's frequency‑dependent loss
  (if you use one to read power on a spectrum analyzer).

The files in **`reference/cal/`** are **example data from the author's bench**
(a ~6–17 W PRO across HF). They illustrate the JSON format and a worked result —
**they are not valid for your setup and must not be used as‑is.** Delete or
replace them with your own calibration. A useful cross‑check while calibrating is
DC‑input efficiency: W_rf / (keyed − idle DC input) should land ≈ 36–47 % for a
class‑AB HF final — wildly outside that means the power figure is wrong.

---

## Architecture

```
solsdr/                   the Python package
  radio.py                High-level Radio: wake + power-on + tune + stream,
                          bidirectional keepalive, auto-reconnect, front-end
                          toggles, dual-receiver (RX2) routing
  wake.py                 Broadcast discovery / wake
  tx.py                   Real-time audio->IQ->paced-UDP TX chain (no PTT)
  tx_session.py           Safety-interlocked TX orchestration (arm/key/drive/deadman)
  server.py               Unified daemon (mock or real radio) + control API + rigctld mirror
  mock_radio.py           Behavioral radio emulator for offline testing
  protocol/
    profiles.py           Per-variant constants (PRO verified; DX from ArtemisSDR)
    control.py            Control socket: freq, mode, PTT, drive, PA, front-end, keepalive
    packet.py             Vectorized IQ codec + discovery/TX packet helpers
    poweron_pro.py        Verified PRO power-on sequence
    tx_pacer.py           timerfd/SCHED_FIFO 5.12 ms packet pacer
    rx_stream.py          RX IQ receive loop
    opcodes.py            Opcode + constant definitions
  dsp/
    demod.py              USB/LSB/AM/FM/CW demod, BFO CW, S-meter, AGC modes
    modulator.py          Audio -> IQ (SSB/AM/FM) for TX, with input leveling
    filters.py            Stateful IQ/audio filters: channel, notch, APF, NB, NR, squelch
    rx_chain.py           Composable RX DSP chain
    cw_decode.py          Morse decoder + Farnsworth encoder
    tx_power.py           Per-band watts<->drive calibration table (TXPowerCal)
    tap_cal.py            RF-sample-tap loss calibration (TapCal), log-f interpolation
  api/
    control_api.py        Text control API (:5556)
    iq_server.py          Raw complex64 IQ TCP stream server, RX (GNU Radio, etc.)
    iq_tx_server.py       Raw complex64 IQ TCP server, TX — client IQ -> radio,
                          interlocked (disarmed unless --tx-arm)
  audio/                  digital-mode bridge (python3 -m solsdr.audio)
    __main__.py           entry point: radio + real rigctld + virtual audio
    js8_bridge.py         RX demod -> virtual sink; app audio -> modulator -> TX
    pulse_devices.py      PulseAudio null sinks + monitor-source remap
    rigctld_poller.py     launches real rigctld + mirrors freq/mode/PTT to the
                          radio — the shared CAT mechanism used by the receiver,
                          server, and audio bridge
solsdr_receiver.py        Main receiver: live control shell + optional servers
clients/                  example IQ client + GNU Radio notes
tools/                    user utilities: FT8 self-test, IQ capture, spectrum,
                          TX power calibration (first-key, anchor, band sweep, tap)
reference/cal/            EXAMPLE calibration data (author's bench — replace with yours)
tests/                    12 test suites
```

---

## Testing

```bash
# Offline suites (no radio needed); TX pacer/session skip without Linux timerfd
for t in test_iq_decode test_codec test_apis test_mock \
         test_iq_server test_iq_tx_server test_tx_pacer test_modulator \
         test_tx_session test_cw test_filters test_js8_bridge; do
    python3 tests/$t.py
done
```

The FT8 self‑test (`tools/ft8_selftest.py`) is the project's ground‑truth RX
check: it records off‑air audio through the full chain and decodes it with
WSJT‑X's `jt9`. Decoded callsigns mean the receiver is genuinely correct, not
just "producing audio." (`test_js8_bridge.py` requires PulseAudio and skips
without it.)

---

## Requirements

- Python 3.10+
- `numpy`, `scipy`, `sounddevice` (see `requirements.txt`)
- An audio backend for `sounddevice` (PipeWire/PulseAudio/PortAudio)
- For the JS8Call/WSJT‑X audio bridge: **PulseAudio/PipeWire** with
  `pactl`/`pacat`/`parec` (Arch: `libpulse`; Debian/Ubuntu: `pulseaudio-utils`)
  and **Hamlib** `rigctld` (Debian/Ubuntu: `libhamlib-utils`)
- A Linux host for TX (the pacer uses `timerfd`; `SCHED_FIFO` wants root or
  `CAP_SYS_NICE`)
- Optional: WSJT‑X (`jt9`) for FT8 self‑validation; `matplotlib` for the
  spectrum tool

---

## License

**GNU General Public License v2.0** — see [`LICENSE`](LICENSE).

This project is built on protocol reverse engineering from
[ArtemisSDR](https://github.com/kk68/ArtemisSDR), which is distributed under the
GNU GPL v2. This project is released under the same GPLv2 to stay aligned with
that lineage; if you redistribute it or derivatives, you must do so under the GPL
and provide complete source.

## Author & acknowledgements

- **Jeff Francis, N0GQ** — author of solsdr.
- **K0KOZ / [ArtemisSDR](https://github.com/kk68/ArtemisSDR)** — SunSDR2 protocol
  reverse engineering; the reference this project is built on.
- **Expert Electronics** — the SunSDR2 PRO hardware.
