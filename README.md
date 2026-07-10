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

> **The core use case is digital modes** (JS8Call, WSJT‑X, fldigi) and
> software‑driven IQ/DSP — that's what solsdr is built and most heavily used for.
> But **voice / SSB phone is also a first‑class transmit mode**, from a PC mic or
> the radio's own **front‑panel Mic1/Mic2 jacks**, keyed by a footswitch,
> spacebar, software, or CAT — all hardware‑verified on‑air (see "Voice / SSB").

**Design: solsdr is engine‑first, GUI‑optional.** The core is a headless,
"front‑panel‑less" command‑line engine meant to be *driven by other software*
over the network — Hamlib (`rigctld`), JS8Call, WSJT‑X, fldigi, GNU Radio, or
your own scripts — with an interactive `sdr>` shell for hands‑on control. It
ships **no built‑in GUI in the core**, but a standalone **panadapter** client
(`clients/panadapter.py`, PyQt/pyqtgraph) gives you a live spectrum + waterfall
and a full click‑to‑tune control panel when you want one — it just talks to the
same network interfaces (RX IQ server + control API) any other client would. So:
run it fully headless as an engine, drive it from digital‑mode software, operate
it by hand at the shell, or add the graphical panadapter — your choice.

Author: **Jeff Francis, N0GQ**.

> ### ⚠️ Maturity — beta core, alpha edges
>
> Maturity varies by feature. **RX1 + TX (CW, digital, and voice/SSB) are
> beta‑quality** — hardware‑verified on‑air and exercised heavily. **RX2
> (second receiver / dual‑watch) is still alpha** — it functions, but not all of
> the plumbing around it is complete, so expect rough edges there. VHF, the DX
> variant, and antenna switching are incomplete/unverified. The project is young;
> **testing, refinement, enhancement, and documentation are ongoing.** Always
> validate TX into a dummy load before going on the air.

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
| **RX2 second receiver** (independent freq/mode; dual-watch) | ⚠️ **alpha** — functions (interleaved on one stream, per-receiver demod/IQ; `--rx2 <kHz>`) but the surrounding plumbing is incomplete; rougher than RX1/TX |
| RX1↔RX2 phase coherence (shared antenna) | ✅ measured **γ²≈0.999** — coherent dual-channel (DF/beamforming viable; see below) |
| External 10 MHz reference (GPSDO) on/off | ✅ opcode verified (bytes identical to ExpertSDR3) |
| Supply telemetry: voltage / current / temperature / forward‑power | ✅ `0x1F` fully decoded; shown in status (V/A, °F). Includes a forward‑power field (`fwd_power_raw`). No SWR is reported by the radio. Temperature is read‑only — the radio runs its own fan in firmware (no host setpoint) |
| Front-end: HF.LPF, VHF.LNA toggles | ✅ opcodes verified (relay‑confirmed); `lpf`/`lna` shell cmds |
| Mic source (Mic1/Mic2/PC) | ✅ verified (`0x21`); `mic` shell cmd. Mic gain is client‑side (no radio cmd) |
| **Voice / SSB phone** (pc / front‑panel mic1 / mic2 sources) | ✅ **hardware‑verified on‑air.** First‑class TX source with per‑source calibrated gain + comms voice shaping. See "Voice / SSB" below. |
| Front‑panel mic (Mic1/Mic2 jack) → on‑air voice | ✅ **hardware‑verified 2026‑07‑10.** `tx source mic1`/`mic2`: the radio digitizes the mic and streams it back as `0xFD`; solsdr modulates it. See ARTEMISSDR.md §7. |
| Radio's external/hardware PTT input (footswitch) | ✅ **hardware‑verified 2026‑07‑10.** `tx hwptt on`: the radio's `0x1F` edge packet keys/unkeys TX. Footswitch + hand‑mic PTT both work. See ARTEMISSDR.md §8. |
| Per‑source mic calibration + comms voice shaping | ✅ `tx cal` (measure → gain), `tx shape flat/comms/dx` (band‑limit + speech compression), saved per source. |
| Adjustable voice‑to‑RF latency | ✅ `tx latency <ms>` (default 120), live + saved. |
| Software / spacebar PTT | ✅ `key`/`unkey`; `voice` = hold‑SPACE push‑to‑talk or tap‑ENTER latched. |
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
| Unified transceiver: one program, RX + in-process TX bridge, live shell control | ✅ shell keys via `tune` / `cw <text>`; `tx` sets power/mode/mic gain/wpm live |
| CW transmit (keyboard sending, Farnsworth) | ✅ `cw <text>` from the shell; `tx wpm <char> [word]`, `tx cwtone <Hz>` |
| Antenna port selection | ⛔ not implemented — PRO opcode not yet identified |
| SunSDR2 DX support | ⚠️ fully coded from ArtemisSDR, **untested on hardware** (`--variant DX`) |

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
- **CW receive decode → shell** — the RX Morse decoder exists but decoded text
  isn't yet surfaced in the transceiver shell (planned). CW *transmit* is done
  (`cw <text>`).

**Expected features not yet built** (a user would reasonably want these):

- **Access control / `--bind`** — the RX IQ server binds all interfaces with no
  authentication, and the control + IQ‑TX servers have no auth (they default to
  loopback). Since solsdr can transmit — including out of band — exposing these
  on a network without a token check or a deliberate bind address is a real risk.
  This is the top item to address before running solsdr exposed.
- **Band‑plan / band‑edge awareness** — solsdr tunes and keys out of band
  silently; expect at least a warning outside amateur allocations.
- **Spectrum / panadapter feed** — solsdr serves raw IQ, but not a ready
  band‑scope (periodic FFT bins) that a thin remote display could consume without
  its own DSP. The most on‑brand missing feature for a no‑GUI SDR.
- **Split / VFO‑A‑B transmit** — RX2 is dual‑*watch* only; no "TX here, RX there".
- **Memory channels / presets** — the config file holds one default; no stored
  recallable list of favorite frequencies/modes.
- **Scanning** — no scan‑a‑range or scan‑memories‑stop‑on‑signal.
- **Decoder output over the network** — the CW decoder prints to the shell but
  isn't exposed on the control API for remote clients.
- **Built‑in recording** — one‑shot IQ capture exists as a tool, but there's no
  scheduled / triggered / rotating‑buffer recording as a server capability.

---

## Install

```bash
# Option A — install the package (gives you the `solsdr` command):
pip install .                          # from a clone; or:  pip install solsdr

# Option B — run from a source checkout without installing:
pip install -r requirements.txt        # numpy, scipy, sounddevice
python3 -m solsdr 14074                 # run it this way from source
```

Installed, the command is **`solsdr`** (alias `solsdr-shell`); from a source
checkout, **`python3 -m solsdr`**. `rigctld` (from `hamlib` / `libhamlib-utils`)
and PulseAudio/PipeWire are required for the TX/digital-mode bridge (it warns and
runs RX-only if they're absent). For an always-on headless setup, see
[`systemd/README.md`](systemd/README.md).

## Quick start

```bash
# Tune 20 m FT8, live audio, type commands at the sdr> prompt.
# One program = full transceiver (RX + TX bridge in-process). --no-tx for RX-only.
solsdr 14074 --device 5                 # if installed (pip install .)
python3 -m solsdr 14074 --device 5      # from a source checkout
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

JSON (`config.json`) — a minimal real‑world example (the author's station file):

```json
{
  "freq_khz": 14074.0,
  "local_ip": "10.1.2.185",
  "mode": "USB",
  "radio_ip": "10.1.2.3",
  "tx_latency_ms": 120.0,
  "tx_mode": "USB"
}
```

`write-config` in the shell snapshots **all** current live settings into this
file — after you calibrate mics and pick shapes, it grows to include the
per‑source voice profiles, TX source, power, etc. A fuller flat `key = value`
config (`config.conf`; values auto‑typed int/float/bool, `#` starts a comment):

```ini
# ~/.config/solsdr/config.conf
radio_ip       = 10.1.2.3      # omit to broadcast‑discover the radio
local_ip       = 10.1.2.185    # omit to bind all interfaces
device         = 5
variant        = PRO
freq_khz       = 14074
mode           = USB
rate           = 39062.5
ext_ref        = true
log_level      = info
# --- transmit / voice ---
tx_mode        = USB
tx_source      = pc            # pc | mic1 | mic2
tx_hwptt       = false         # key from the radio's external PTT input
tx_latency_ms  = 120           # voice->RF pre-buffer (lower = less delay)
tx_watts       = 5
max_power_watts = 10
# per-source voice profiles (written by `tx cal` / `tx shape` / `write-config`)
tx_src_pc_gain    = 2.3
tx_src_pc_shape   = comms      # flat | comms | dx
tx_src_pc_cal     = true
tx_src_mic2_shape = flat
```

With a config in place, `solsdr` (no args) tunes 20 m USB on the right radio; add
a frequency to override just that: `solsdr 7074`. Point at a different file with
`--config /etc/solsdr/config.conf` (handy for the systemd service). Unknown keys
are ignored with a warning, so a shared config can carry extra keys.

---

## Interactive shell — commands with examples

**`solsdr` is one program for both RX and TX.** By default it brings the
digital‑mode/TX bridge up **in‑process** (virtual audio + a real Hamlib rigctld +
PTT→transmit), sharing the one radio connection with the receiver. So the single
`sdr>` shell controls the whole radio — receive, DSP, front‑end, *and* transmit
characteristics — with TX changes applied **live** (e.g. `tx power 3` retunes the
drive on an in‑progress over). `--no-tx` reverts to a lean RX‑only program. Type
`help` at the prompt for the full list. Bare commands act on RX1; prefix with
`2 ` to target the second receiver (see [RX2](#second-receiver-rx2--dual-watch)).

| Command | Example | Effect |
|---|---|---|
| `<kHz>` | `7074` | Tune to 7074 kHz |
| `m <mode>` | `m CW` | Set mode: USB / LSB / AM / FM / CW / CWU / CWL |
| `cw on\|off` | `cw on` | Toggle the live Morse decoder |
| `cw pitch\|bw <Hz>` | `cw pitch 700` | CW beat‑note pitch / filter bandwidth |
| `agc <mode>` | `agc off` | RX audio AGC: `auto`/`on`/`off`/`fixed:<gain>` |
| `gain <n>` / `vol <n>` | `gain 8000` | RX audio output level (fixed gain, AGC off) |
| `tx` | `tx` | Show all TX settings (live) |
| `tx power <W>` | `tx power 3` | TX output setpoint — **applies live while keyed** |
| `tx maxpower <W>` | `tx maxpower 5` | Amp‑protection ceiling (safety; next over only) |
| `tx mode <m>` | `tx mode USB` | TX modulation mode |
| `tx source <s>` | `tx source mic2` | Voice TX audio source: `pc` (the `‑tx` sink) / `mic1` / `mic2` (front‑panel mic). Each keeps its own gain + shape. |
| `tx hwptt on\|off` | `tx hwptt on` | Key TX from the radio's external/footswitch PTT input |
| `tx cal [s]` | `tx cal` | ⚠️ Calibrate the current source's mic gain (talk normally `s` s; mic1/mic2 **key the radio** to measure). Saved per source. |
| `tx shape [src] <p>` | `tx shape pc comms` | Voice shaping preset: `flat`/`comms`/`dx` (band‑limit + speech compression). Per source. |
| `tx gain [src] <x>` | `tx gain pc 2.3` | Manually set a source's input gain (marks it calibrated) |
| `tx micgain <x>` | `tx micgain 1.5` | Extra TX‑audio gain, all sources — applies live |
| `tx latency <ms>` | `tx latency 60` | Voice→RF pre‑buffer (lower = less delay hearing yourself; default 120). Live + saved. |
| `tx wpm <c> [w]` | `tx wpm 25 15` | CW send speed — element `c` wpm, optional Farnsworth spacing `w` wpm |
| `tx cwtone <Hz>` | `tx cwtone 700` | CW send sidetone/pitch (default 600) |
| `tx prefix <name>` | `tx prefix myrig` | Rename the virtual audio devices → `<name>-rx.monitor` (fldigi/WSJT‑X input) + `<name>-tx` (output). Also settable at launch with `--prefix`. |
| `key` / `unkey` | `key` | ⚠️ Software PTT: key / unkey TX with the current `tx source` (aliases `ptt`/`unptt`) |
| `voice` | `voice` | ⚠️ Push‑to‑talk console: **HOLD SPACE** to talk, **tap ENTER** to latch on/off, `q`/`Esc` exits |
| `cw <text>` | `cw cq de n0gq` | ⚠️ **Transmit** text as Morse |
| `tune [s] [W]` | `tune 5 3` | ⚠️ **Key a CW tuning carrier** for `s` sec (default 3) at `W` watts (default current power). |
| `s` | `s` | Print S‑meter + full status line |
| `ref ext\|int` | `ref ext` | External 10 MHz (GPSDO) vs internal reference |
| `lpf on\|off` | `lpf on` | HF low‑pass filter relay |
| `lna on\|off` | `lna on` | VHF LNA |
| `preamp <state>` | `preamp -10` | RX preamp/att: `-20` `-10` `0` `+10` dB, or `off`/`preamp` |
| `mic <src>` | `mic pc` | Mic source at the radio: `mic1` / `mic2` / `pc` |
| `rit <Hz>` | `rit 250` | Receiver incremental tuning (`rit 0` = off) |
| `nr <0-1>` | `nr 0.4` | Noise reduction strength |
| `nb <0-1>` | `nb 0.3` | Noise blanker |
| `notch <Hz>` | `notch 800` | Manual notch (`notch 0` = off) |
| `apf <0-1>` | `apf 0.6` | Audio peak filter (CW) |
| `sql <0-1>` | `sql 0.2` | Squelch threshold |
| `devices` | `devices` | List audio devices (sounddevice + PulseAudio) |
| `read-config` | `read-config` | Apply every setting in the config file, now |
| `write-config` | `write-config` | Save all current live parameters to the config |
| `2 <cmd>` | `2 m CW` | Run a per‑receiver command against RX2 (e.g. `2 7074`) |
| `q` | `q` | Quit |

TX settings take effect immediately (live). They're saved to the config file
with `write-config` and reloaded on startup — including per‑source mic gains,
voice shaping, the TX source, hardware‑PTT enable, and `tx latency`. To transmit
digital modes, point a digital‑mode app (JS8Call / WSJT‑X / fldigi) at the
bridge's virtual audio + rigctld; it's the same process as the receiver, so the
shell sees and controls the transmit state directly.

**Pinning RX audio output (PipeWire).** `--device 5` is the `pipewire` device,
which follows the *default* sink — so plugging in a USB mic/headset can steal
your RX audio onto it. To pin RX to a fixed output regardless of hotplug, set
`PULSE_SINK` to a stable sink name (from `pactl list short sinks`), e.g.:

```bash
PULSE_SINK=alsa_output.pci-0000_00_1b.0.analog-stereo solsdr
```

---

## CLI flags — common use cases

```bash
# Plain listening: 40 m LSB, audio out on device 5
solsdr 7185 --mode LSB --device 5

# Point at a specific radio / interface (overrides the config file)
solsdr 14074 --radio-ip 10.1.2.3 --local-ip 10.1.2.185

# The RX IQ server is ON BY DEFAULT (:5555) — GNU Radio / panadapter / recorders
# can connect straight away. Just tune:
solsdr 14074

# Expose CAT to WSJT-X / fldigi / JS8Call (real rigctld on :4532)
solsdr 14074 --hamlib

# Disable the RX IQ server if you need the port free / to reduce load
solsdr 14074 --no-iq-server

# Accept raw IQ from GNU Radio and TRANSMIT it (tcp :5558). No RF without --tx-arm.
solsdr 14074 --iq-tx-server                        # wiring test, no RF
solsdr 14074 --iq-tx-server --tx-arm --max-power-watts 5 --tx-watts 3

# IQ + control API are ON BY DEFAULT; here just add a wider 312.5 kHz IQ rate
solsdr 14074 --rate 312500
# (disable a default server if you must: --no-iq-server / --no-control-api / --no-tx)

# Dual-watch: RX1 20 m (audio + IQ :5555), RX2 40 m CW (IQ :5557)
solsdr 14074 --rx2 7025 --rx2-mode CW

# External 10 MHz reference (GPSDO) on / off
solsdr 14074 --ext-ref
solsdr 14074 --no-ext-ref

# Headless for a fixed time then exit (no prompt) — scripting/capture
solsdr 14074 --seconds 60

# Move the CAT or IQ ports
solsdr 14074 --hamlib --hamlib-port 4540 --iq-port 5560

# SunSDR2 DX (UNVERIFIED profile) and quieter logging
solsdr 14074 --variant DX --log-level warning

# Show version
solsdr --version
```

`--iq-port` sets RX1's IQ port; RX2's is always RX1+2 (default 5557). Full list:
`solsdr --help`.

---

## Panadapter (live spectrum + waterfall)

`clients/panadapter.py` is a standalone, visually-nice panadapter that reads the
RX IQ stream (and, optionally, the control API for live radio state). Pure
Python — PyQt5/PyQt6/PySide6 + pyqtgraph + numpy — no GNU Radio, no ExpertSDR3.
**Display only:** it never tunes or keys the radio.

```bash
# on the radio host (RX IQ + control API are both on by default):
solsdr 14074

# then, anywhere that can reach it (needs a display; ssh -X for remote):
python3 clients/panadapter.py --host 127.0.0.1

# no radio? replay a recorded capture (loops for a hands-off demo):
python3 clients/panadapter.py --file clients/examples/solsdr_20m_demo30.iq
```

Features: FFT spectrum over a scrolling waterfall on a shared absolute-frequency
axis; frequency across the bottom (MHz) and level up the side (dBFS, or dBm via
`--ref-offset`); auto-scale or fixed ref/range; auto-adapts to solsdr's sample
rate + center and follows retunes; perceptual colormaps for strong signal/noise
contrast; live mouse crosshair readout (freq + level); an info bar with tuned
freq, mode, PTT, TX power, S-meter, span, and RBW; a draggable splitter to trade
spectrum vs. waterfall height; **frequency zoom** (`+`/`−`/`Full` toolbar buttons,
centered on the tuned freq); averaging, peak-hold, DC-spike hide, FFT size, and
freeze. Keys: `+`/`-` zoom (`0` = full), `A` auto-scale, `R` rescale now,
`P` peak-hold, `C` cycle colormap, `space` freeze, `Q` quit. Run
`python3 clients/panadapter.py --help` for options.

> Performance: it's tuned to hit 30 fps+ on a **CPU-only** box. The two costs
> that matter in software rendering are re-ranging the axis and an antialiased
> filled trace — so auto-scale recomputes the range only every few seconds
> (`--rescale SEC`, default 5; press `R` to snap now) and the trace is a thin
> non-antialiased line by default. On a GPU/fast machine, `--pretty` restores a
> filled antialiased trace.

> Absolute power: solsdr RX isn't power-calibrated, so the axis is **dBFS** by
> default. If you've measured your front-end offset, `--ref-offset <dB>` relabels
> it as approximate dBm.

---

## GNU Radio — IQ in and out

solsdr exposes the radio to GNU Radio (or any SDR tooling) two ways: **raw IQ
over TCP** for receive, and **audio into the digital‑mode bridge** for transmit.

### Receiving IQ (radio → GNU Radio)

The RX IQ server is on by default, so just tune and connect a **TCP source**:

```bash
solsdr 14074 --rate 312500        # complex64 IQ on tcp 0.0.0.0:5555 (on by default)
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

The **raw‑IQ TX server** is the transmit counterpart of the RX IQ server: it
accepts raw `complex64` baseband IQ over TCP and streams it to the radio verbatim
(gain + clip only — no modulation, no resampling), so your flowgraph *is* the
modulator. Unlike the RX server it is **opt‑in** (`--iq-tx-server`) — transmit is
always a deliberate act.

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
out the antenna, and RX (:5555, on by default) + TX (`--iq-tx-server`, :5558)
give you a symmetric complex‑IQ pipe in and out of the radio.

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
python3 -m solsdr 14074 --rx2 7074
#   RX1 -> audio + IQ on :5555   |   RX2 -> IQ on :5557  (both on by default)
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

## Voice / SSB — a first‑class TX source

solsdr does SSB/AM/FM **phone** as a first‑class transmit mode, from any of three
audio sources, keyed however you like. All of it is hardware‑verified on a PRO
(on‑air, into a dummy load). solsdr transmits **host‑modulated IQ** — the mic
audio (whichever source) runs through solsdr's own `Modulator` and is streamed to
the radio as `0xFD` IQ — so voice, digital modes, and CW all share one TX chain
and one set of safety interlocks.

### Three TX audio sources (`tx source`)

| Source | What it is | Notes |
|--------|-----------|-------|
| `pc`   | Audio on the `solsdr‑tx` PulseAudio sink — a computer mic routed in, or any app's output | Default. No RF needed to set up; calibrate any time. |
| `mic1` | The radio's **front‑panel Mic1** jack | The radio digitizes the mic and streams it back to the host, which modulates it. Selecting it also sets the radio mic source. |
| `mic2` | The radio's **front‑panel Mic2** jack (e.g. a Yaesu hand mic) | Same mechanism as mic1. |

Set the source in the shell: `tx source pc` / `mic1` / `mic2`. Each source keeps
its **own** calibrated gain and voice‑shaping preset (below), saved in the config
file and re‑applied automatically when you switch — so a hand mic on mic2 and a
studio USB mic on pc each "just work" at their own settings.

### Keying: hardware PTT, software key, spacebar, or CAT

There is **no VOX**; you key deliberately, any of these ways:

- **Hardware PTT** — a footswitch or hand‑mic PTT wired to the radio's rear‑panel
  PTT input. Turn it on with `tx hwptt on`; the radio reports each PTT edge to the
  host and solsdr keys/unkeys from it.
- **Software key** — `key` to transmit, `unkey` to stop (aliases `ptt`/`unptt`).
- **`voice` (hands‑on‑keyboard)** — enter `voice` for a push‑to‑talk console:
  **HOLD SPACE** to talk (release to unkey), or **tap ENTER** to toggle a latched
  (hands‑free) over on/off; `q`/`Esc` exits. Uses the current `tx source`.
- **CAT PTT** — any Hamlib client (JS8Call/WSJT‑X/fldigi/`rigctl`) with PTT = CAT
  pointed at `127.0.0.1:4532` (this is how digital modes key).

All of them fire the same interlocked `TXSession` (arming, dead‑man, drive
ceiling, calibration‑gated amp limit). **Validate into a dummy load first.**

### Connecting a USB / computer mic to `pc`

`pc` transmits whatever is on the `solsdr‑tx` sink, so loop your mic into it. With
the bridge running:

```bash
# Find your mic's PulseAudio source name:
pactl list short sources          # e.g. alsa_input.usb-Blue_Microphones-00.analog-stereo

# Route it into the TX sink the modulator reads (low latency):
pactl load-module module-loopback \
    source=alsa_input.usb-Blue_Microphones-00.analog-stereo \
    sink=solsdr-tx latency_msec=30

# In the solsdr shell:
#   tx source pc
#   tx cal            # calibrate your gain (talk normally ~4 s; no RF for pc)
#   voice             # hold SPACE to talk

# When done, unload the loopback (id is printed by load-module, or list it):
pactl list short modules | grep loopback
pactl unload-module <id>
```

### Mic gain calibration (`tx cal`)

Instead of guessing a mic‑gain number, **calibrate**: `tx cal` measures your
normal speaking level for a few seconds and computes the gain that puts your
voice at a sensible level with headroom. It's saved per source.

- **`pc`** calibrates from the `solsdr‑tx` sink with **no transmit**.
- **`mic1`/`mic2`** can only be measured *while transmitting* (the radio only
  streams the front‑panel mic while keyed), so `tx cal` **keys the radio** for the
  measurement window — **dummy load required**. It says so before keying.

A calibrated source uses a **fixed** input gain (like a real rig's mic gain);
an uncalibrated one falls back to auto‑leveling. You can also set a gain by hand:
`tx gain [pc|mic1|mic2] <value>`.

### Voice shaping for comms (`tx shape`)

SSB is a communications mode, not hi‑fi. Each source has a shaping preset that
band‑limits the audio and adds speech compression for intelligibility and talk
power (a studio/USB mic picks up rumble, proximity bass and sibilance that just
waste your ~3 kHz of SSB bandwidth):

| Preset | Band | Compression | For |
|--------|------|-------------|-----|
| `flat`  | 300–2700 Hz | none  | a mic already voiced for comms (Yaesu hand mic) |
| `comms` | 250–2800 Hz | 6 dB  | studio / USB mics (default for mic1 & pc) |
| `dx`    | 350–2700 Hz | 12 dB | maximum readability in a pileup (punchy) |

Set per source: `tx shape pc comms`, `tx shape mic2 flat`, etc. Defaults: mic2 =
`flat`, mic1/pc = `comms`.

### Latency (`tx latency`)

The voice‑to‑RF delay (how long until you hear yourself on a monitor receiver) is
dominated by the IQ pre‑buffer ahead of the transmit pacer. Default **120 ms**;
lower it for less delay, raise it if you hear clicks/gaps:

```
tx latency 60         # 60 ms — snappier, more underrun risk
tx latency 250        # safer on a loaded box
```

Saved in config as `tx_latency_ms`. Front‑panel mic (mic1/mic2) audio arrives
over the network, so it may need a slightly higher value than a local `pc` mic
before it's click‑free.

Use `--monitor <sink>` (or `tx` to see all settings) to hear the exact audio
being modulated while you dial these in.

---

## Radio control (CAT) — Hamlib `rigctld` is required

solsdr does **not** implement the rigctld wire protocol itself. Control software
(WSJT‑X, fldigi, JS8Call, Log4OM, `rigctl`, …) talks to a **real Hamlib
`rigctld`**, and solsdr connects to that same rigctld as a second client,
mirroring its frequency/mode/PTT to the radio over UDP. This means all the
finicky CAT/PTT/split capability negotiation is Hamlib's own battle‑tested code,
not a reimplementation — the tradeoff is that **`rigctld` must be installed**
(Debian/Ubuntu: `libhamlib-utils`; Arch: `hamlib`).

Both `solsdr --hamlib` and `python3 -m solsdr.audio` launch the
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
    audio_bridge.py       RX demod -> virtual sink; app audio -> modulator -> TX
    pulse_devices.py      PulseAudio null sinks + monitor-source remap
    rigctld_poller.py     launches real rigctld + mirrors freq/mode/PTT to the
                          radio — the shared CAT mechanism used by the receiver,
                          server, and audio bridge
solsdr/cli.py             Transceiver shell: RX + in-process TX bridge + servers
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
