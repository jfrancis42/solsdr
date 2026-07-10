# Findings for the ArtemisSDR project

This project ([solsdr](README.md)) is built on the SunSDR2 protocol
reverse engineering done by **K0KOZ in [ArtemisSDR](https://github.com/kk68/ArtemisSDR)** —
full credit and thanks. ArtemisSDR was the reference for discovery, power‑on,
control, tuning, and IQ framing.

Because ArtemisSDR's authors state they had only a SunSDR2 **DX** to test with,
several **PRO‑specific** details in their code are extrapolated from the DX. We
have a real PRO and captured ExpertSDR3 against it, so this file collects the
PRO findings that differ from — or aren't present in — the ArtemisSDR reference,
in case they're useful upstream. Everything here was verified on real PRO
hardware (mostly on HF; VHF is largely untested here).

Offered back in the same spirit ArtemisSDR's work was shared with us. All values
little‑endian; "control packet" = 18‑byte header, opcode at byte 2.

---

## 1. PRO IQ sample rate — issues #46 / #47 (the missing capture)

**Where ArtemisSDR is stuck (as of v2.1.7 / issue #47):** lifting the PRO from
39062.5 Hz to a higher rate (issue #46) was attempted in v2.1.7 by swapping the
eight per‑channel rate‑code words from `0x14` to the DX's `0x32`. On real PROs
that broke RX — Bernie F6Bernie's #47 report ("no sound at all… panafall
extremely slow… receiver seems completely deaf"), matching earlier reports from
Jim W4JEA, Pedro EA5CCY, and SQ5OMO — so v2.1.7 was reverted. The hotfix notes
state the blocker plainly: *"We don't have a verified wire capture of EESDR3
commanding the PRO at 312500 to ground‑truth the correct rate code."*

**Here is that capture / the ground truth.** The eight `0x14` rate‑code words are
**not** the rate selector on the PRO — they stay constant across all four rates
(so changing them to `0x32` is why RX broke: it corrupts the state‑sync template
without actually selecting a rate). The real selector is a **rate *index*** in
the STATE_SYNC (`0x01`) packet at **byte offsets 56 and 58** (two `uint16` LE
fields carrying the same value):

| index | rate |
|-------|------|
| 0 | 39062.5 Hz |
| 1 | 78125 Hz |
| 2 | 156250 Hz |
| 3 | 312500 Hz |

**How this was verified:** captured ExpertSDR3 on a real PRO stepping the rate
39→78→156→312→39 kHz; the index field at bytes 56/58 cycled `0→1→2→3→0` in exact
lockstep while the eight `0x14` words never changed. Commanding the index
directly (leaving the `0x14` words alone) streams at exactly the requested rate,
and FT8 decodes cleanly at 312500 Hz on the PRO. So the DX‑vs‑PRO difference is
**not** a different rate code in the same field — it's a different field
entirely; the PRO uses this index while the `0x14` words are fixed formatting.

**One downstream gotcha once the higher rate works:** at the higher rates, a
per‑block FFT `resample()` in the audio path smears tones at the large decimation
ratio (~26:1 for 312.5 kHz → 12 kHz) and silently kills FT8 decode even though
the panadapter looks fine — which can masquerade as "rate set but no usable
audio." Use a polyphase `resample_poly` (or a staged decimator) with a proper
anti‑alias FIR instead of FFT resample.

(Field offsets are into the STATE_SYNC payload as this project frames it; if the
ArtemisSDR state‑sync template is indexed differently, the two `uint16` fields
are the pair immediately after the RX‑count byte — see §4b, byte 54 — i.e. bytes
54 = RX count, 56/58 = rate index.)

---

## 2. Reference clock (external 10 MHz / GPSDO) — opcode `0x1D`

Not implemented for the SunSDR2 in the ArtemisSDR reference.

- **Opcode `0x1D`**, u32 payload: `1` = external 10 MHz (GPSDO), `0` = internal.
- Verified by toggling ExpertSDR3's **Ext.Ref** button and watching the payload
  cycle `01`/`00` in exact lockstep with the on/off states.
- The PRO's power‑on init sequence sends `0x1D=1`, i.e. it **boots expecting an
  external reference**.

---

## 3. Supply telemetry (voltage / current / temperature) — packet `0x1F`

The radio streams a small periodic status packet on the **RX stream port
(50002)**, header `<magic> ff 00 1f` (note: opcode `0x1F` is at **byte 3**, with
byte 2 = `0x00` — unlike the byte‑2 opcode of the control packets). Verified
against the radio's own display (13.6 V, ~1.1 A, ~41 °C):

| offset | type | scale | field |
|--------|------|-------|-------|
| 8  | uint16 | raw | **forward power** (0 at RX, rises with TX output) |
| 10 | uint16 | ×455 | supply current again (redundant copy of offset 14) |
| 12 | uint16 | — | fixed reference ~4088 (12‑bit full‑scale marker, 0xFFF) |
| 14 | uint16 | ÷100 | supply current (A) |
| 16 | uint16 | ÷10 | supply voltage (V) |
| 18 | float32 | ×1 | temperature (°C) |

**All fields resolved 2026‑07‑07.** A keyed drive‑sweep into a dummy load
(offsets logged both RX and TX) settled the three formerly‑unknown uint16s:

- **offset 8 = forward power.** Exactly `0` during RX; on key‑down it jumps up
  and rises monotonically with TX drive/output. It is **nonlinear** (from 20 m
  data neither a clean watts nor √watts scale) and, critically, there is **no
  companion reflected‑power field anywhere in the packet** — so the radio does
  **not** report SWR here. (We explicitly tested the SWR hypothesis: treating
  off8/off10 as fwd/refl gave 1.26:1 at one power — tantalisingly close to the
  1.2:1 dummy load — but across a drive sweep the derived "SWR" drifted
  1.07→1.38, and a dummy load's SWR is power‑independent, so it cannot be SWR.
  The single‑point match was coincidence.) off8 is still useful as the radio's
  own "am I making power" indicator, independent of any external tap.
- **offset 10 = supply current, second copy.** off10/off14 is a dead‑constant
  **455** at every drive level. Redundant with offset 14; not S‑meter, not SWR.
- **offset 12 = fixed reference** ~4088, never moves.

Neither drifting field was ever the S‑meter (an earlier RX experiment already
ruled that out: IQ power swung ~9 dB quiet↔busy and across a −20/−10/0/+10 dB
preamp‑att sweep with no field response). **The S‑meter is computed in the GUI
from IQ, not reported in this packet** — an IQ‑derived S‑meter is the correct
approach. ExpertSDR3's SWR reading must come from a directional coupler via a
packet/path not captured here.

---

## 4. Front-end toggles — `0x1B` is HF.LPF (not RX2), VHF.LNA on `0x05`

Captured ExpertSDR3's front-end buttons one at a time on a real PRO (HF freq):

- **HF.LPF** → opcode **`0x1B`**, u32 `1` = LPF engaged, `0` = auto. **Note:**
  the ArtemisSDR reference labels `0x1B` as `RX2_ENABLE`; on the PRO this is
  what the HF.LPF button drives. (RX2's real opcode is still TBD here — the DX
  labeling may not hold for the PRO.)
- **VHF.LNA** → opcode **`0x05`** (the preamp/att opcode), byte‑18 `0x82` = LNA
  on, `0x02` = off. Confirmed by an audible relay click on each toggle. `0x05`
  otherwise carries the preamp/att states `0x80`–`0x83`; the LNA uses the
  `0x82`/`0x02` pair.
- **BPF** → **no wire command** on an HF frequency. ExpertSDR3's auto/BPF button
  produced nothing on the control socket across repeated toggles, so BPF
  selection appears to be automatic/frequency‑derived (handled in the app or by
  the radio) rather than an explicit command — at least on HF.

## 4a. Mic source + mic gain

- **Mic source** → opcode **`0x21`**, byte‑18: `0` = Mic1, `1` = Mic2. Only two
  values exist on the wire. **The GUI's third option, "PC", sends the *same*
  `0x21=1` as Mic2** — verified by an isolated single Mic1→PC click, which sent
  exactly `0x21` byte‑18 `01`. So the radio does not distinguish PC from Mic2;
  the PC‑vs‑Mic2 choice is handled in ExpertSDR3's software audio routing, not
  on the wire. (Matches ArtemisSDR's `0x21` packet, default `1`=Mic2.)
- **Mic gain / preamp level** → **no wire command.** Sweeping the mic gain
  (0 dB → −20 → +80 → +10 → 0) produced *nothing* on the control socket across
  the whole capture. ExpertSDR3 applies mic gain as a **software gain on the TX
  audio stream**, not a radio register — so implementers should scale the
  outgoing TX IQ, not send a command.

## 4b. RX2 (second receiver) — a STATE_SYNC field, not a toggle opcode

RX2 enable is **not** a standalone command. Toggling it in ExpertSDR3 triggers a
full stream teardown + re‑init (a `0x02` POWER_OFF followed by the whole init
sequence), and the actual selector is a **field in the STATE_SYNC (`0x01`)
packet at byte 54**: `0x02` = RX2 on (two receivers), `0x01` = RX2 off (one).
Verified by an on/off/on/off capture (byte 54 cycled `02`/`01`). This is the
"number of receivers" field, sitting just before the rate‑index bytes (56/58).
(So `0x1B`, which the DX‑derived reference calls RX2_ENABLE, is *not* RX2 on the
PRO — it's HF.LPF; see §4.)

### The RX2 IQ stream (how the second receiver's samples arrive)

Characterized 2026‑07‑08 on a real PRO (RX1 on busy 20 m FT8, RX2 on a dead
band). Both receivers stream on the **same port 50002** — no second port — as
**interleaved packets tagged in the RX IQ header**:

- **byte 8 = active‑receiver count:** `01` when one receiver is running, `02`
  when two. (Mirrors the STATE_SYNC byte‑54 enable — every IQ packet is stamped
  with how many receivers are active.)
- **byte 9 = receiver index:** `00` = RX1, `01` = RX2. **This is the
  discriminator** for routing a packet to the right receiver.

(For reference the full RX IQ header is: `[0]` magic `01`, `[1]` `ff`, `[2]`
opcode `fe`, `[3]` `ff`, `[4:6]` payload length 1200 LE, `[6:8]` sequence,
`[8]` count, `[9]` index.)

How it was verified: with one receiver, 100 % of IQ packets carry byte8=`01`,
byte9=`00`. Enabling RX2 doubled the radio→host packet rate and split it exactly
50/50 into byte9 `00` and `01` (byte8 now `02`), with a shared per‑packet
sequence counter and the two receivers alternating. Amplitude confirmed the
index→receiver mapping: byte9=`00` tracked the busy FT8 band (peaks ~−95 dBm),
byte9=`01` the empty band (~−115 dBm) — index 0's IQ magnitude ran consistently
higher at every percentile.

**Keepalive with two receivers.** The `0xFE` silence echo is sent **once per
sequence tick, not once per packet.** Measured: single‑RX = ~122 pkt/s received,
~122/s echoed (1:1); with RX2 on = ~329/s received total (~165/s per receiver)
but still only ~165/s echoed. Since the two receivers share the sequence counter
and alternate, the clean rule is **echo only on byte9 == 0 (RX1)** — that yields
exactly one echo per tick in both 1‑RX and 2‑RX modes. Echoing per‑packet sends
2× the pokes (probably harmless, but not what ExpertSDR3 does).

Implemented in this project: `decode_iq_packet_rx()` returns `(rx_index,
samples)` from byte 9; the RX loop routes per index to per‑receiver demods,
echoes keepalive on index 0, and delivers a 2‑arg `callback(rx_index, iq)`;
enable via byte 54 = `0x02` at power‑on; RX2 tuned with the freq sub‑index
(RX1=0, RX2=1). The `0x1B`‑is‑HF.LPF vs. RX2 confusion (§4) is unrelated — RX2
has no toggle opcode at all.

### RX1↔RX2 phase coherence (verified, γ² ≈ 0.999)

With a single shared antenna/ADC feeding both DDCs, the two receivers are
**strongly phase‑coherent** — magnitude‑squared coherence **γ² ≈ 0.999** at the
signal. So a PRO can serve as a coherent dual‑channel receiver (DF, beamforming,
two‑antenna noise cancelling) — with two caveats found in testing:

- **Measure it spectrally, at the signal.** Tune both RX to the same frequency,
  Welch‑average the cross‑spectrum, and take γ²(f) = |Sxy|²/(Sxx·Syy) in the
  strongest bins. A whole‑band time‑domain average on a weak/bursty signal is
  swamped by uncorrelated noise and *falsely* reads γ≈0.26 even when locked
  (the tell: phase jitter stays ~4–10° throughout). The per‑bin method reads
  0.999.
- **The fixed phase offset does NOT survive a stream restart/retune** (measured
  −69°/−61°/−178° across runs), so coherent applications need a **per‑session
  phase calibration** against a common reference, not a baked‑in constant.
- Coherence is a same‑frequency property: RX on two different bands correlate at
  γ²≈0 (different signals), as expected. Real DF also needs two *separate*
  antennas — the shared‑antenna coherence is what makes the inter‑antenna phase
  difference meaningful once you split the feed.

## 5. Other PRO facts we found differ from the reference

- **Control source port must be 50001.** The client must *bind* source port
  50001; the radio ignores control traffic from any other source port (even
  though discovery works from an ephemeral port).
- **PRO native rate is 39062.5 Hz** (195 pkt/s), not the DX's 312500.
- **PRO RX stream is bidirectional on 50002:** the client must echo one silence
  packet per received RX packet, or the radio stops streaming after ~8 s.
- **PRO TX IQ goes to port 50002, NOT 50003.** This was verified the hard way
  during TX bring‑up: TX IQ (`0xFD`) sent to 50003 produced no RF; the same
  packets to **50002** (the RX port) work — the `0xFD` TX frames replace the
  `0xFE` RX keepalives on the shared port while keyed, exactly as ExpertSDR3
  does it. (An earlier assumption of a separate 50003 TX port was wrong.)
- **PRO DDC offset is 0**, not the DX's 92.5 kHz. On the PRO, PRIMARY (`0x09`)
  and COMPANION (`0x08`) are both the display frequency; using the 92.5 kHz
  offset produced only noise.
- **Verified PRO power‑on init sequence** is captured in
  `solsdr/protocol/profiles.py` (`_PRO_INIT`) — it differs from the
  extrapolated `power_on_macro_pro[]` (e.g. the STATE_SYNC template values and
  ordering).

---

## 6. TX bring-up notes (PRO)

Findings from getting the PRO to actually transmit, in case they help an
upstream TX implementation:

- **TX‑entry command order that works:** config‑block `0x20` with the TX/mode
  byte → drive `0x17` → MOX `0x06=1`. The `0x20` config block is **required**
  before keying; without it the radio keys but emits only carrier bias / no
  modulated output. Exit is the reverse: PA off → MOX `0x06=0` → config‑block
  back to RX.
- **TX IQ replaces the RX keepalive on 50002** (see §5) — `0xFD` frames on the
  same socket the client uses for the `0xFE` echoes.
- **Drive `0x17` is a raw 0–255 byte**, and output is **not** linear in the byte:
  power tracks roughly drive² (a voltage‑like control). The manufacturer's
  sqrt‑of‑watts encoding is a reasonable default, but real output must be
  wattmeter‑calibrated per band.
- **The PRO is a ~15 W‑class radio and its output is band‑dependent** — measured
  ~6 W on 160 m rising to ~17 W on 10 m at full drive (into a dummy load,
  internal PA, external‑PA line disconnected). It is *not* flat and *not* 100 W.
- **External‑PA / PTT‑out key line:** MOX (`0x06`) asserts the radio's rear‑panel
  external‑PA key. There is **no separate opcode to suppress it** — `0x24`
  (`PA_ENABLE`) is effectively a no‑op on the PRO and does **not** gate the
  external‑amp key. Any keying asserts that line. (ExpertSDR3's "key external PA"
  GUI option did not function as documented in the firmware tested.)
- **`0x1F` offset 8 is a forward‑power reading** (see §3) — usable as the radio's
  own "am I making power" indicator during TX, though it's uncalibrated and there
  is no companion reflected‑power field (so no SWR from telemetry).

## 7. Front‑panel mic → the mic is digitized and streamed to the HOST (verified 2026‑07‑10)

The SunSDR2 does **not** modulate the front‑panel mic internally. When you key
with a mic in Mic1 or Mic2, the radio **digitizes the mic and streams that audio
up to the host**, the host software modulates it, and streams complex TX IQ back
down. Verified by capturing ExpertSDR3 transmitting SSB from Mic2 (handheld) and
Mic1 (footswitch), 20 m USB into a dummy load:

- **During key‑down, `0xFD` frames flow BOTH directions on 50002** (RX `0xFE`
  stops entirely both ways — 0 `fe` frames either direction while keyed):
  - **radio → host `0xFD` = the mic audio.** Same 1210‑byte frame format as RX IQ
    (header `01 ff fd ff b0 04 …`, 200 samples, 24‑bit), but it's a **real mono
    signal**: every I/Q pair is identical (measured **I==Q on 100.0%** of pairs).
    So the radio ships mono mic audio in the IQ frame with both channels set equal.
  - **host → radio `0xFD` = the modulated TX IQ.** Genuine **complex** baseband
    (**I==Q on 0.0%** of pairs) — the host modulator's SSB output, sent verbatim to
    the PA (the raw‑IQ TX path solsdr already implements).
- `0xFD` frames exist **only** in the keyed window (0 outside it), both directions.

### Downstream mic‑audio frame layout (radio → host, `0xFD`)

Byte‑identical framing to an RX IQ packet, only the opcode differs:

```
offset  bytes  meaning
0       1      magic (PRO 0x01)
1       1      0xFF
2       1      0xFD   <-- opcode: TX‑active (RX IQ is 0xFE)
3       1      0xFF
4..5    2      payload length, little‑endian = 1200
6..7    2      sequence (uint16 LE)
8       1      (as in RX IQ headers)
9       1      (as in RX IQ headers)
10..1209 1200  200 samples × 6 bytes: 24‑bit LE signed, Q first then I
```

Decode is the SAME 24‑bit Q‑first unpack as RX IQ — but the payload is a **mono
audio** signal duplicated into both channels (**I == Q on 100 % of samples**,
measured). So take EITHER channel as the mono mic sample and normalise by 2²³:

```python
# pairs: (200, 6) uint8; cols 0..2 = Q, 3..5 = I (identical here)
i = decode_24le_signed(pairs[:, 3:6]).astype(float32) / 8388608.0   # mono mic
```

Total 1210‑byte packets, 200 samples each, arriving at the wire rate
(≈39062.5 Hz for the PRO). ~195 packets/s.

### How to transmit the front‑panel mic — the full loop

It is a **feedback loop on port 50002**, entirely host‑side modulation:

```
key (footswitch or CAT)                 radio digitizes selected mic
        │                                        │
        ▼                                        ▼
  radio enters TX  ──────────────────▶  radio → host  0xFD  (mono mic audio)
        ▲                                        │
        │                                        ▼
  host → radio  0xFD (complex SSB IQ) ◀── host Modulator (audio→IQ)
        │
        ▼
     PA → RF
```

Implementation steps (this is what solsdr does):

1. **Select the jack:** send `0x21` = 0 (Mic1) or 1 (Mic2) — §4a. This tells the
   radio which mic to digitize.
2. **Key normally:** the verified TX‑entry sequence (§6) — config‑block `0x20`
   (TX) → drive `0x17` → MOX `0x06`=1 — and STOP echoing the `0xFE` RX keepalive
   (RX IQ ceases both directions while keyed). No special "mic mode" command; the
   downstream `0xFD` mic stream just starts once keyed.
3. **Consume downstream `0xFD`** on 50002, decode to mono audio (above).
4. **Modulate** that audio (SSB/AM/FM) → complex baseband IQ at the wire rate.
   Because the mic audio is ALREADY at the wire rate, the modulator's "audio
   rate" = the wire rate (identity resample — do NOT resample from 48 kHz).
5. **Send upstream `0xFD`** — the standard TX‑IQ path (the same `0xFD` frames the
   raw‑IQ TX server / GNU Radio path uses), paced at the 5.12 ms cadence.
6. **Unkey** with the verified exit sequence (§6); RX IQ resumes.

`0x21` selects *which jack the radio digitizes*; ExpertSDR3's GUI "PC" option
reuses Mic2's wire value (`0x21`=1) and simply **ignores** the upstream mic
stream, modulating soundcard audio instead (see §4a). This whole scheme mirrors
TCI's RX_AUDIO/TX_AUDIO split. The single non‑obvious gotcha for an
implementer: you must KEY FIRST — the mic audio only flows after MOX‑on — so
prebuffer zero and let the modulator feed silence until the first mic packet
arrives (don't block waiting for audio before keying, or you deadlock).

## 8. External PTT input is reported to the host via a `0x1F` edge packet (verified 2026‑07‑10)

The radio's rear‑panel **external‑PTT input** (footswitch / handheld PTT button)
is **readable over the network** — the host does not poll for it; the radio pushes
an unsolicited status packet on each edge. Verified by pressing a footswitch wired
to the radio (no mic audio) and watching 50002:

**Packet header — how to tell it from supply telemetry.** Both this PTT‑edge
packet and the streaming supply telemetry (§3) carry opcode `0x1F` at **byte 3**.
They are distinguished by **byte 2** (call it the subtype):

```
byte:   0     1     2     3     ...
        magic 0xFF  SUB   0x1F  ...
                    ^^^
  supply telemetry: SUB = 0x00   (V/A/temp/fwd‑power, §3)
  external‑PTT edge: SUB = 0x01   (this packet)
```

They also differ in size (telemetry's UDP payload is larger), but **byte 2 is the
reliable discriminator** — match `magic, 0xFF, 0x01, 0x1F` for the PTT edge and
`magic, 0xFF, 0x00, 0x1F` for telemetry. (Earlier notes cited "64 vs 76 bytes";
those were tshark on‑wire frame lengths including Ethernet/IP/UDP headers — the
subtype byte is what you actually test in a decoder.)

- **PTT state is a byte at payload offset 18:** `0x01` = **pressed** (key down),
  `0x00` = **released** (key up). Sent once per transition, not continuously.
- **Timing (why ExpertSDR3 keys off it):** the radio's *press* packet arrives
  **3–13 ms BEFORE** the host's own key command (`0x18`→`0x20`→`0x06`). Measured
  across four presses: press→host‑key gaps of 13, 9, 8, 3 ms. The host reads this
  packet, then decides to key. There is **no polling opcode** — the host only ever
  sends `0x18/0x20/0x06`; the radio initiates.

### How to implement external PTT

Trivial once you're already reading 50002:

1. In the RX loop, before falling through to telemetry, test each non‑IQ packet
   for the PTT‑edge header (`byte2==0x01 && byte3==0x1F`). Read the state byte at
   offset 18 → pressed/released.
2. On a **pressed** edge, run your normal key sequence (§6); on **released**,
   unkey. This is a hardware‑PTT source that sits alongside CAT PTT — either can
   key. (solsdr gates it behind an opt‑in flag so a stray footswitch can't
   transmit unless enabled.)
3. Keying does blocking control‑socket I/O, so dispatch it off the RX loop (a
   short‑lived thread) — never stall the loop that must keep echoing keepalives.

Also make sure the telemetry parser rejects the PTT‑edge packet (require
byte 2 == 0x00) so a key press isn't misread as a voltage/current reading.

---

## Resolved since first writing (for reference)

- **RX2 second receiver — done.** Enable, per‑receiver tuning, IQ‑stream
  demux (header byte 9), keepalive, and phase coherence are all worked out and
  implemented; see §4b.
- **RX during TX — neither receiver streams while keyed (2026‑07‑08).** Bench
  test into a dummy load with RX1 (20 m) + RX2 (40 m) both active: keying RX1
  dropped BOTH receivers' packet rate from ~195 pkt/s each to ~1 pkt/s for the
  key‑down, resuming instantly on unkey. This follows directly from §5 — the
  `0xFD` TX IQ frames *replace* the RX IQ on the shared port 50002, so the radio
  is either streaming RX or accepting TX, not both. There is no receive‑through‑
  transmit on the PRO; RX2 is for dual‑*watch* between overs.
- **TX is general‑coverage — NOT ham‑band‑locked (2026‑07‑08).** Keyed at
  13000 kHz (non‑ham) into a dummy load with raw drive: the radio tuned without
  refusal and made RF (forward‑power telemetry rose, DC current 1.1→2.6 A). No
  firmware band restriction on TX. Two notes: off‑band forward power reads lower
  for the same DC draw (no band‑specific output match), and the tune/key path
  reported no error off‑band — so an application, not the radio, must enforce any
  band‑edge policy.
- **Fan / temperature — nothing to send.** The radio regulates its fan
  autonomously in firmware. The fan cycles on its own while solsdr runs (solsdr
  sends no fan/temp command), and a 2026‑07‑08 control‑socket capture while
  trying to change the setting in ExpertSDR3 showed **zero directed host→radio
  commands** — the installed ExpertSDR3 only *displays* temperature. Host temp
  is read from the `0x1F` telemetry (§3). (An operator recalled ExpertSDR2
  allowing a temperature set; if it existed it was removed/changed in v3.)

## Still to capture / confirm

- **Antenna port selection.** ArtemisSDR uses `0x15` (+ `0x1E` preamble); a PRO
  capture showed `0x15` staying `00` while `0x1e`/`0x20` moved, so the PRO
  mapping differs. Needs a clean one‑selector‑at‑a‑time recapture — not pursued
  further yet.
- **RX2 phase‑offset repeatability across power cycles.** Within a session the
  offset is fixed‑but‑not‑repeatable across stream restarts (§4b); whether a
  full power‑cycle changes anything about the coherence relationship is
  untested.
- **DX hardware verification.** The DX profile here is populated from the
  ArtemisSDR reference but has never run against a real DX.
