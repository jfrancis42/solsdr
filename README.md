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
| External 10 MHz reference (GPSDO) on/off | ✅ opcode verified (bytes identical to ExpertSDR3) |
| Supply telemetry: voltage / current / temperature / forward‑power | ✅ `0x1F` fully decoded; shown in status (V/A, °F). Includes a forward‑power field (`fwd_power_raw`). No SWR is reported by the radio |
| Front-end: HF.LPF, VHF.LNA toggles | ✅ opcodes verified (relay‑confirmed); `lpf`/`lna` shell cmds |
| Mic source (Mic1/Mic2/PC) | ✅ verified (`0x21`); `mic` shell cmd. Mic gain is client‑side (no radio cmd) |
| USB / LSB / AM / FM / CW demodulation + S‑meter | ✅ verified |
| CW receive: BFO demod + Morse decoder | ✅ verified (synthetic full‑chain) |
| Automatic reconnection on network loss | ✅ verified (simulated interruption) |
| CAT control via real Hamlib `rigctld` (mirrored to the radio) | ✅ verified with JS8Call/WSJT‑X/`rigctl` |
| Raw‑IQ TCP server + text control API (GNU Radio client) | ✅ verified |
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
- **RX2 second receiver** — the enable field is known (STATE_SYNC byte 54), but
  handling the second IQ stream isn't implemented.
- **VHF** — largely untested; deliberately never keyed during TX calibration.
- **SunSDR2 DX** — profile is coded from ArtemisSDR but unverified on hardware.
- **CW transmit** — not built (the encoder exists).

---

## Quick start — receive

```bash
pip install -r requirements.txt        # numpy, scipy, sounddevice

# Receive 20 m FT8 with live audio; type commands at the sdr> prompt
python3 solsdr_receiver.py 14074 --device 5
```

Interactive commands while running:

```
<kHz>              tune (e.g. 7074)              nr <0-1>    noise reduction
m <mode>           USB/LSB/AM/FM/CW/CWU/CWL      nb <0-1>    noise blanker
cw on|off          live Morse decode            notch <Hz>  notch filter (0=off)
s                  S-meter + status             apf <0-1>   audio peak filter (CW)
ref ext|int        10 MHz reference             sql <0-1>   squelch
lpf on|off         HF low-pass filter           mic <src>   mic source
q                  quit
```

Expose the radio to other software:

```bash
python3 solsdr_receiver.py 14074 --hamlib       # CAT control via real rigctld on :4532
python3 solsdr_receiver.py 14074 --iq-server    # raw complex64 IQ on tcp :5555
python3 solsdr_receiver.py 14074 --rate 312500  # wider IQ (39062.5/78125/156250/312500)
```

- **GNU Radio (IQ):** TCP Source (Complex Float 32) at `host:5555`; read the
  sample rate from the one‑line text header. See `clients/README.md`.
- **WSJT‑X / fldigi (control):** Radio = "Hamlib NET rigctl", server
  `127.0.0.1:4532`. `--hamlib` requires **Hamlib's `rigctld`** to be installed —
  solsdr does not implement the rigctld protocol itself, it launches a real one
  and mirrors its state to the radio (see the CAT section below).

`--device N` selects the audio output (list with
`python3 -c "import sounddevice as sd; print(sd.query_devices())"`). On a
PipeWire/PulseAudio desktop use the `pipewire`/`pulse` device, **not** a raw
`hw:` ALSA device.

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
                          bidirectional keepalive, auto-reconnect, front-end toggles
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
    iq_server.py          Raw complex64 IQ TCP stream server (GNU Radio, etc.)
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
tests/                    11 test suites
```

---

## Testing

```bash
# Offline suites (no radio needed); TX pacer/session skip without Linux timerfd
for t in test_iq_decode test_codec test_apis test_mock \
         test_iq_server test_tx_pacer test_modulator test_tx_session \
         test_cw test_filters test_js8_bridge; do
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
