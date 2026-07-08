# 100 GNU Radio project ideas for the solsdr IQ stream

solsdr hands GNU Radio a clean complex64 IQ firehose — up to **312.5 kHz** of
spectrum (~150 kHz usable) from the SunSDR2 PRO over TCP. GNU Radio is a DSP
LEGO set on top of that. This is a categorized, annotated backlog.

**The stream:** `solsdr_receiver.py <kHz> --iq-server` publishes little-endian
interleaved float32 I,Q (numpy complex64) on TCP :5555, preceded by a one-line
text header (`SOLSDR IQ rate=... fmt=complex64 freq=...`). `qt_iq_waterfall.py`
in this directory shows the pattern for consuming it (the `_SolsdrTCPSource`
block).

**Effort key:** 🟢 easy / a weekend · 🟡 moderate / a few days · 🔴 ambitious /
a real project. **Tags:** **RX2** needs the second receiver (enable is
reverse-engineered — `ARTEMISSDR.md` byte 54 — but its IQ stream isn't wired
yet) · **TX** needs a TX-IQ intake into solsdr · **GPSDO** leans on the locked
10 MHz reference for absolute-frequency accuracy.

---

## A. Panadapter & multi-signal — the core win

The defining advantage of streaming raw IQ: ~150 kHz of spectrum at once means
you can *see and work several signals simultaneously* from a single capture,
which a conventional single-VFO radio simply cannot do.

### 1. Combined FFT + waterfall viewer 🟢 (done)
Already built (`qt_iq_waterfall.py`): one `qtgui.sink_c` showing the PSD and
spectrogram on a shared frequency axis so peaks line up with waterfall hotspots.
This is the "is it working?" demo and the reference for every other flowgraph —
it establishes the connection, header parse, and the `_SolsdrTCPSource` block.

### 2. Adjustable display gain / y-axis / update rate 🟢 (done)
The `--gain`, `--update`, and axis controls that make solsdr's low-amplitude IQ
visible and the refresh snappy. Kept as a lesson: solsdr IQ peaks near −90 dBFS,
so anything visual needs a level lift, and anything *measuring* levels needs
calibration (same lesson as the TX-power work).

### 3. Click-to-tune panadapter 🟡
Click anywhere on the spectrum and a `freq_xlating_fir_filter` + demod chain
retunes to that offset within the captured band — instant QSY without touching
the radio's dial, because you're just moving a software filter inside the IQ.
This is the single most satisfying demo: the "wait, it can do *that*?" moment,
and the natural next build on top of the waterfall.
Use cases: casual band-cruising, chasing a signal you spotted on the waterfall,
teaching how SDR tuning actually works. Effort is moderate — the DSP is a
standard xlating-filter + resampler + demod, the work is the GUI click-to-offset
plumbing and keeping the audio chain glitch-free on retune.

### 4. Multiple simultaneous receivers 🟡
Tap the same IQ at several offsets with parallel xlating filters, each feeding
its own demod — decode five SSB QSOs across 20 m at once, or monitor CW + FT8 +
a voice net concurrently. One radio behaving like a rack of them.
Use cases: net control monitoring several frequencies, DX spotting while ragchew,
comparing signals. Moderate effort; the pattern is just "instantiate the chain N
times," but audio routing/mixing for N streams needs thought (see #5).

### 5. N-signal audio mixer 🟡
Take the multiple receivers from #4 and pan them across a stereo field (or
route each to a different output/virtual sink) so you can actually *listen* to
several at once without them mushing together — spatialized monitoring.
Use cases: situational awareness on a busy band, "left ear is the net, right ear
is the DX." Moderate; the DSP is trivial, the UX of managing N channels is the work.

### 6. Bookmark bar 🟢
Labeled vertical markers on the panadapter at known frequencies (nets, beacons,
calling frequencies, that repeater input). Purely a display overlay driven by a
small JSON/YAML of freq→label.
Use cases: never hunt for the SSTV calling frequency again; visual band plan.
Easy — it's annotation on top of the existing FFT widget.

### 7. Signal-list sidebar 🟡
Auto-detect peaks above the noise floor and list them (offset, absolute freq,
power, rough width) in a side panel, click-to-tune from the list. A
machine-readable version of "what's on the band right now."
Use cases: quick triage of a band, feeding a logger/spotter, accessibility.
Moderate — peak detection + de-bounce so the list doesn't flicker.

### 8. Sub-band zoom / decimate 🟢
Decimate to a narrow slice for finer resolution — zoom the waterfall into a few
kHz to separate two close signals or examine a signal's fine structure.
Use cases: splitting a pileup, examining CW keying/chirp, RTTY mark/space.
Easy — a decimating FIR + re-centered display.

### 9. Dual panadapter (wide + zoomed) 🟡
Two stacked displays: one showing the full ~150 kHz, one zoomed on the tuned
signal — overview and detail at once, like a good spectrum analyzer.
Use cases: keep band context while working a signal. Moderate — two sink chains
with a linked tuning cursor.

### 10. Persistence / "phosphor" FFT 🟡
Max-hold plus slow decay so intermittent signals (a quick CQ, a data burst)
leave a visible trace instead of vanishing between frames — analog-scope
phosphor emulation.
Use cases: catching bursty/weak signals the eye would miss on a live trace.
Moderate — an accumulate-and-decay buffer feeding a custom draw or heatmap.

### 11. Adjustable FFT window / size UI 🟢
Live controls for FFT size and window function (rectangular, Hann,
Blackman-Harris, flat-top) so you can trade resolution vs. leakage vs. amplitude
accuracy on the fly.
Use cases: teaching windowing tradeoffs; picking the right window for a
measurement vs. a pretty display. Easy — expose the sink parameters.

### 12. Mouse frequency readout 🟢
Show the absolute RF frequency (and dB) under the cursor as you move across the
panadapter, in real Hz (using the header's center freq + `enable_rf_freq`).
Use cases: "what frequency is that?" without math. Easy overlay.

### 13. Full scripted SDR console 🔴
The whole enchilada: tune, mode, filter width, S-meter, memories, band buttons,
click-to-tune, bookmarks — a complete operating GUI built from the pieces above,
driving solsdr's control path (rigctld) for the actual radio.
Use cases: a genuinely usable headless-radio front end for people who *do* want
a console. Ambitious — it's integrating a dozen of the other ideas into one
coherent app, plus the control-side wiring.

---

## B. Demodulation (analog)

solsdr already demodulates internally, but doing it in GNU Radio lets you A/B
approaches, prototype improvements, and build exactly the chain you want.

### 14. AM demod 🟢
Envelope or synchronous AM for the broadcast bands, aviation (once you're near
those), or AM ragchews. `blocks.complex_to_mag` for envelope; a Costas/PLL for
synchronous.
Use cases: SWL, AM nets, comparing envelope vs. synchronous on a fady signal.
Easy — a handful of standard blocks.

### 15. SSB (USB/LSB) 🟢
Weaver or Hilbert-method single-sideband — the bread-and-butter HF voice mode.
Use cases: everyday HF listening; the baseline every other voice tool builds on.
Easy; GR has the pieces, and solsdr's own SSB is a reference to check against.

### 16. CW with adjustable BFO 🟢
Beat the carrier down to an audible pitch with a tunable BFO and a narrow filter;
adjustable pitch and bandwidth for comfort and crowded conditions.
Use cases: CW operating/listening; front-end to the CW skimmer (#25). Easy —
frequency shift + narrow filter + tone.

### 17. NBFM demod 🟢
Narrowband FM for 10 m FM simplex/repeaters (and 2 m once VHF is supported).
`analog.quadrature_demod` is the whole trick.
Use cases: 29.6 MHz FM, local repeaters. Easy.

### 18. Synchronous AM with carrier PLL 🟡
Lock a PLL to the carrier so selective fading doesn't cause the distortion that
envelope detection suffers — noticeably cleaner AM on HF.
Use cases: better SWL/AM copy under fading. Moderate — a stable carrier-recovery
loop is the fiddly part.

### 19. Independent-sideband (ISB) demod 🟡
Demod upper and lower sidebands separately into two audio channels — used for
some utility/broadcast signals, and a neat demonstration of what SSB discards.
Use cases: ISB utility stations, teaching. Moderate.

### 20. Adjustable DSP filter bank 🟡
Runtime-variable bandpass: drag the passband edges, steepness, and center shift
and hear the effect immediately on the received signal.
Use cases: digging a signal out of QRM, teaching filtering. Moderate — stable
re-parameterization of the filter without clicks.

### 21. GR-vs-solsdr demod A/B 🟡
Run the same IQ through a GR demod and solsdr's `dsp/demod.py`, switch between
them (or diff the audio) to evaluate quality and catch regressions.
Use cases: development QA, picking the better algorithm. Moderate plumbing.

### 22. AGC tuning playground 🟢
Expose attack/decay/hang/reference on a real signal to build intuition and pick
good defaults — the kind of thing that's abstract until you can turn the knobs
live.
Use cases: tuning solsdr's own AGC; teaching. Easy.

---

## C. Decoding ecosystem — fan one capture out to many decoders

The trick a hardware radio can't do: **N decoders from one 150 kHz slice.** Split
the IQ into narrowband channels and hand each to its own decoder.

### 23. Whole-band FT8 multi-decode 🟡
FT8 lives in a ~3 kHz sub-band, but a wide capture spans more than one mode's
watering hole. Split the IQ into channels and feed each to its own `jt9`,
decoding multiple FT8 slots (or FT8 across bands if you capture cleverly) at once.
Use cases: maximize decodes, DXpedition spotting, propagation studies. Moderate —
channelize + resample to 12 kHz + drive jt9 (solsdr's FT8 self-test shows the jt9
plumbing already).

### 24. FT4 / JS8 concurrent decode 🟡
Same channelizer feeding FT4 and JS8 decoders alongside FT8 — one capture, three
mode ecosystems monitored simultaneously.
Use cases: complete digital-mode situational awareness on a band. Moderate,
mostly wiring more decoders onto the #23 channelizer.

### 25. CW skimmer 🔴
Energy-gate every CW signal across the band, run each through a Morse decoder
(solsdr has one in `dsp/cw_decode.py`), timestamp and spot the decoded calls —
your own local Reverse Beacon Network node.
Use cases: contest/DX spotting, propagation, "who's on 40 m CW right now."
Ambitious — reliable multi-signal CW detection + decoding is genuinely hard
(QRM, varying speeds, drift), but hugely rewarding.

### 26. Callsign enrichment 🟡
Cross-reference decoded calls (from the skimmer or digital decoders) against the
`govt-data` API (10.1.0.20:8091 `/callsigns`, batch endpoint available) to add
name, QTH, grid, license class to each spot.
Use cases: richer spot displays, logging assist, "is that a new one?" Moderate —
it's an API call per batch of calls; the API and SSID-awareness already exist.

### 27. PSK31 / PSK63 decoder farm 🟡
Park several PSK decoders on the watering holes and decode all the ongoing QSOs
at once.
Use cases: PSK monitoring, activity logging. Moderate — GR has PSK demod; the
decode-to-text layer is the work (or pipe to fldigi instances).

### 28. RTTY decoder 🟡
45.45-baud Baudot FSK — still used in contests and by some utility stations.
Use cases: RTTY contests, utility monitoring. Moderate — FSK demod + Baudot.

### 29. MFSK / Olivia / Contestia 🟡
The robust multi-FSK modes popular for weak-signal keyboard QSOs.
Use cases: weak-signal ragchew monitoring. Moderate; you have an Olivia modem in
`olivia-modem/` to reference or bridge to.

### 30. Feld Hell decoder 🟡
The fuzzy-mode that paints text as an image — decode to a scrolling bitmap.
Use cases: Hell activity, a fun demo. Moderate.

### 31. HF APRS (300 baud) 🟡
Decode the 30 m HF APRS channel and gate positions/messages into the existing
`aprs-server` / `aprs-dog` pipeline.
Use cases: HF APRS coverage, feeding your APRS ecosystem from HF. Moderate — 300
baud AFSK demod → APRS parser (you have `stinky`/`aprs-dog` parsers to reuse).

### 32. SSTV decoder 🟡
Decode 20 m SSTV (14.230 calling) to PNG — Scottie/Martin/Robot modes.
Use cases: catching SSTV pictures, contests, ISS SSTV (VHF later). Moderate —
sync detection + line timing.

### 33. HF weatherfax (WEFAX) 🟡
Decode marine/aviation weatherfax charts to images.
Use cases: offline weather charts, a classic HF utility. Moderate — similar
mechanics to SSTV.

### 34. Audio → virtual sink → external decoder 🟢
Route any GR demod's audio to a PulseAudio virtual sink so external
fldigi/WSJT-X/JS8Call can decode it — reuses the pattern solsdr's audio bridge
already established.
Use cases: leverage mature decoders without reimplementing them. Easy — you've
built this plumbing once already.

### 35. One capture → many external decoders 🔴
Generalize #34: fan a single capture out to *many* external decoder instances
parked at different offsets, each with its own virtual audio device.
Use cases: a full digital-mode monitoring station from one radio. Ambitious —
device management and process orchestration for N decoders is the real work.

### 36. NAVTEX / DSC 🟡
Maritime data (490/518 kHz NAVTEX, HF DSC channels) — if you point the radio at
the marine segments.
Use cases: maritime monitoring. Moderate — specific FSK/ARQ decoders.

### 37. Automatic mode-ID 🔴
Classify each detected signal's modulation (AM/SSB/CW/FSK/PSK/FT8…) automatically
so the panadapter can label signals or auto-launch the right decoder.
Use cases: hands-free band monitoring, feeding #35. Ambitious — feature
extraction + classifier; overlaps with #85.

---

## D. Measurement & receiver characterization (rf-bench)

Plays directly to the rf-bench instruments and the calibration discipline from
the TX-power work. solsdr becomes a *measurement receiver*.

### 38. MDS (minimum discernible signal) 🟡
Drive the antenna port from the Siglent SDG (via the rf-bench driver), reduce
level to the noise floor, and measure the SunSDR2's MDS from the captured IQ —
the receiver-test bench with solsdr as the instrument.
Use cases: quantifying receiver sensitivity, before/after preamp changes.
Moderate — automate SDG level sweep + IQ SNR measurement.

### 39. Two-tone IMD → live IP3 🟡
Inject two tones, watch the third-order products in the FFT, compute IP3 in real
time as you vary level.
Use cases: receiver linearity/overload characterization. Moderate — the FFT peak
picking + IP3 math; needs a clean two-tone source (SDG, or solsdr TX #65).

### 40. 1 dB compression / blocking sweep 🟡
Sweep a strong off-channel signal and measure when the desired signal's gain
compresses — blocking dynamic range.
Use cases: real-world strong-signal performance. Moderate — coordinated
two-source sweep.

### 41. Image rejection / IF-shape measurement 🟡
Map the front-end/IF filter response and image suppression by sweeping a source
across the passband and edges.
Use cases: characterizing the radio's filtering, verifying config. Moderate.

### 42. Phase-noise / reciprocal-mixing check 🟡
Measure close-in phase noise / reciprocal mixing against a clean SDG carrier.
Use cases: LO quality, comparing to spec. Moderate — careful measurement setup;
overlaps with the rf-bench phase-noise project.

### 43. S-meter calibration curve 🟢
Build an IQ-power → dBm curve against a known-level source so solsdr's S-meter
reads true. Mirrors the TX-power calibration approach on the RX side.
Use cases: trustworthy signal reports, absolute measurements. Easy-ish — one
source, a level sweep, curve fit.

### 44. Noise figure (Y-factor) 🟡
With a noise source, measure NF via the Y-factor method.
Use cases: sensitivity in absolute terms, preamp evaluation. Moderate — needs a
noise source and hot/cold measurement.

### 45. Band noise-floor logging 🟢
Log power-per-bin (or per-segment) over time to a file — the raw material for
occupancy studies and "is my noise floor rising?" monitoring.
Use cases: RFI hunting, site noise characterization, propagation. Easy —
integrate FFT frames, write CSV/SQLite.

### 46. GPSDO WWV frequency-error monitor 🟡 **GPSDO**
With the 10 MHz reference locked (opcode `0x1D`), measure WWV/WWVH's carrier
offset to sub-Hz and log it over time — a standing calibration check for the
whole bench and a check on the GPSDO itself.
Use cases: frequency standard validation, drift tracking. Moderate — a tight PLL
on the WWV carrier + logging.

### 47. Antenna A/B SNR comparison 🟡
Log per-bin SNR while switching RX antennas to quantify which antenna wins on
which band/azimuth.
Use cases: antenna decisions with data instead of vibes. Moderate — *and* gated
on antenna-port selection being reverse-engineered (a current TODO).

### 48. Filter/preamp/attenuator characterization 🟡
Measure the effect of any inline device (external filter, preamp, attenuator) by
before/after captures.
Use cases: evaluating station accessories. Moderate.

### 49. Front-end health check 🟢
Spectral flatness, DC offset, and IQ-imbalance measurement on the received
stream — a quick "is the front end behaving?" diagnostic.
Use cases: catching IQ imbalance (shows as mirror images), DC spikes. Easy —
standard IQ-quality metrics.

### 50. Long-term radio frequency-drift log 🟢 **GPSDO**
Track the radio's own frequency accuracy over time against the locked reference —
warm-up drift, long-term stability.
Use cases: instrument-grade confidence in the radio. Easy given the reference.

---

## E. Monitoring, logging & networked

Turn the receiver into an always-on sensor feeding your existing infrastructure
(MQTT bus, SQLite loggers, maps, SMS alerts).

### 51. Band-occupancy % over time → SQLite 🟢
Compute how much of a band is occupied per interval and log it.
Use cases: "when does 20 m open?", RFI trends. Easy — threshold the FFT, integrate.

### 52. Publish power-per-bin to MQTT 🟡
Push spectrum/occupancy data to the rf-bench MQTT bus (`rf_bench.mqtt`, broker
10.1.0.20:1883) so other tools/subscribers can consume it.
Use cases: integrating the radio into the bench ecosystem, remote dashboards.
Moderate — you have the MQTT client library and envelope conventions.

### 53. Grafana "when does each band open?" dashboard 🟡
Visualize the occupancy time series (via the MQTT→SQLite logger you already run)
as a per-band opening heatmap.
Use cases: propagation planning at a glance. Moderate — mostly Grafana config on
top of #51/#52.

### 54. Waterfall recording to PNG / timelapse 🟡
Save the waterfall to images or a timelapse video — a 24-hour band recording in
one frame.
Use cases: propagation documentation, spotting periodic RFI. Moderate — buffer +
image/video encode.

### 55. NCDXF/IARU beacon monitor 🟡
Track the international beacon network cycling through 20/17/15/12/10 m; log
which beacons you hear and when → open-path record.
Use cases: real propagation data, antenna comparison. Moderate — timed windows +
carrier/CW detection at the beacon frequencies.

### 56. Propagation heatmap on the maps server 🔴
Combine beacon/decoded-spot data with the `maps` PMTiles server to plot open
paths geographically in real time.
Use cases: a genuinely useful propagation map from your own station. Ambitious —
data pipeline + map integration (you have the map server and MapLibre pieces).

### 57. Browser panadapter (WebSocket) 🟡
Stream FFT frames over a WebSocket to an HTML5 canvas so the waterfall is
viewable from a phone/laptop with no X forwarding — same pattern as the rf-bench
virtual instruments.
Use cases: check the band from the couch; remote monitoring. Moderate — a small
WebSocket server + canvas renderer.

### 58. Scheduled / triggered SigMF recorder 🟡
A rotating SigMF ring buffer with power-threshold and time-of-day triggers, so
band openings or unusual bursts auto-capture — mirrors the rf-bench rtlsdr
recorder design.
Use cases: catch-it-while-it's-there recording, forensics. Moderate.

### 59. "New signal" alerter 🟢
Notify (SMS via `money/sms.py` → voip.n0gq.org) when energy appears where there
usually isn't — intruder/beacon/opening alerts.
Use cases: unattended monitoring of a quiet segment. Easy — occupancy delta +
your existing SMS path.

### 60. Contest activity meter 🟡
Trend signals-per-band-per-hour to see a contest ramp up or a band come alive.
Use cases: contest strategy, band selection. Moderate — peak counting over time.

### 61. Intruder watch 🟡
Log signals appearing in exclusive/quiet amateur segments (IARU Monitoring
System style).
Use cases: band-defense documentation. Moderate — segment definitions + logging.

### 62. Daily band-condition summary 🟢
Auto-generate a daily email/summary from the occupancy log ("20 m opened
0930–2200Z, 40 m busy overnight").
Use cases: a personal propagation newsletter. Easy — cron + report from #51 data.

---

## F. TX side (GNU Radio → solsdr transmit)  ⚠️

TX works now (audio→IQ→paced wire, per-band calibrated). GNU Radio can *generate*
the IQ, becoming the modulator — solsdr needs a small TX-IQ intake to accept it.
**Every TX idea goes through the arm/deadman/amp-limit interlocks and per-band
calibration: dummy load + wattmeter first, always.**

### 63. Arbitrary-waveform TX 🟡 **TX**
Generate any waveform in a flowgraph — multitone, chirp, shaped pulse, custom
digital mode — and stream it out. Pairs with the `rf-bench sunsdr/tx-arb` idea.
Use cases: test signals, experimental modes, propagation soundings. Moderate —
the TX intake is the new piece; waveform gen is easy in GR.

### 64. GR-native digital-mode transmitter 🟡 **TX**
Transmit PSK31/RTTY/MFSK generated in GR, keyed through the interlocks.
Use cases: TX side of the decoder farm, experimental modes. Moderate.

### 65. Two-tone TX source 🟡 **TX**
Generate a clean two-tone signal for the RX-side IMD/IP3 measurements (#39) —
solsdr transmitting into a dummy load, measured on the SSA or looped to RX. Full
closed-loop transmitter characterization without a second radio.
Use cases: TX linearity/IMD measurement, bench self-test. Moderate.

### 66. WSPR beacon 🟡 **TX GPSDO**
Scheduled, GPSDO-disciplined WSPR — frequency-accurate thanks to the reference
lock. Harvest spots from WSPRnet/RBN for a real propagation study of your antenna.
Use cases: propagation research, antenna evaluation, low-power fun. Moderate —
WSPR symbol generation + precise timing (you have period-sync experience from
the JS8/CW projects).

### 67. Scheduled propagation beacon 🟡 **TX**
A low-power beacon with a custom ID on a schedule, for reverse-beacon/skimmer
harvesting.
Use cases: propagation, antenna tests. Moderate.

### 68. Transverter IF driver 🔴 **TX**
Generate a clean IF that a transverter upconverts — turning solsdr into a signal
source on VHF/UHF/microwave.
Use cases: microwave weak-signal, EME experiments, wideband work above HF.
Ambitious — TX intake + transverter integration + level discipline.

### 69. Loopback transmitter characterization 🟡 **TX**
TX a known signal into a dummy load, capture it on RX (or the SSA), and measure
spectral purity, harmonics, ALC behavior, and the drive→power curve — extends the
existing TX-power calibration into full TX characterization.
Use cases: verifying clean transmit, catching splatter. Moderate — you already
have the calibration harness to build on.

### 70. TX/RX test harness 🔴 **TX**
Transmit a known signal and measure it live on RX for automated end-to-end tests
(modulation quality, frequency accuracy, power).
Use cases: regression testing the whole TX chain. Ambitious — coordination +
safety.

---

## G. Contest / DX / operating

### 71. Multi-signal CQ spotter 🟡
Skim the whole band for CQ calls (CW skimmer + phone/digital decoders), enrich
via `govt-data`, and flag new/needed stations — a personal RBN for your band.
Use cases: DX/contest spotting without internet spots. Moderate — combines
#25/#26 with a "needed?" filter against your log.

### 72. Pileup visualizer 🟡
During a DXpedition pileup, the panadapter + per-signal SNR shows exactly where
stations are stacking up (up 1–5 kHz), so you can drop your call in a clear spot.
Use cases: bust pileups efficiently. Moderate — #4/#7 focused on the split window.

### 73. SO2R-lite dual-watch 🟡 **RX2**
With RX2 wired, watch two bands (or two ends of one) at once — run frequency on
one, hunt mults on the other.
Use cases: contesting, DX. Moderate — but gated on the second IQ stream.

### 74. Auto-logging assist 🟢
Feed decoded call + timestamp + measured frequency into your log (or push to
MQTT for a station dashboard).
Use cases: less manual logging, accurate frequencies. Easy once decoders exist.

### 75. Personal RBN → station dashboard 🟡
Publish local skimmer spots to the MQTT bus and render a live "heard here"
dashboard.
Use cases: know your own propagation in real time. Moderate — #25 + #52.

### 76. Split-finder 🟡
Automatically highlight the clearest frequency to transmit in a pileup based on
the live occupancy of the split window.
Use cases: faster QSOs in pileups. Moderate — occupancy minimum within a window.

### 77. Run-frequency defender 🟢
Alarm if someone starts transmitting on your run frequency while you're away from
the radio (e.g. between contest exchanges).
Use cases: hold your frequency. Easy — energy watch on a narrow window + alert.

---

## H. Novel signal-processing plays

### 78. Blind carrier PLL-lock + drift tracker 🟡
Lock a PLL to any carrier and log its frequency vs. time to sub-Hz — watch a
transmitter's TCXO warm up, or measure path Doppler.
Use cases: transmitter fingerprinting, oscillator studies. Moderate — a robust
PLL + logging.

### 79. Ionospheric Doppler / spread monitor 🔴 **GPSDO**
Track WWV's carrier and read out moment-to-moment Doppler shift and spectral
spread — a direct measure of ionospheric motion/disturbance (a poor-man's
ionosonde). GPSDO lock makes the absolute numbers trustworthy.
Use cases: space-weather/propagation science, TID detection. Ambitious — precise
carrier tracking + spectral-spread estimation + interpretation.

### 80. Adaptive noise blanker / QRM canceller 🟡
Prototype adaptive filters, impulse noise blankers, and heterodyne cancellers in
GR, then port the winners into solsdr's own DSP.
Use cases: better copy in noise; improving solsdr. Moderate — LMS/adaptive
filtering.

### 81. Two-antenna coherent noise canceller 🔴 **RX2**
With two coherent channels (RX2), subtract a noise antenna from the main antenna
to null local QRM — the real version of an MFJ noise canceller, in software.
Use cases: killing local power-line/switching noise. Ambitious — needs coherent
RX2 + adaptive cancellation.

### 82. Passive radar / meteor-scatter pinger 🔴
Watch a distant broadcast/beacon carrier for reflections; meteor scatter shows as
brief carrier pings — an automated Perseids counter, or aircraft/rain scatter
detection.
Use cases: meteor counting, propagation research, "look ma, radar." Ambitious —
detection + classification of transient reflections.

### 83. Cross-correlation TDOA / AoA 🔴 **RX2**
Two coherent channels → time-difference-of-arrival, angle-of-arrival, or
interferometry between two antennas.
Use cases: direction finding, transmitter geolocation. Ambitious — coherent RX2
+ correlation processing + geometry.

### 84. Carrier Doppler on reflections 🟡 **GPSDO**
Measure Doppler on aircraft/ionospheric-path reflections of a stable carrier.
Use cases: aircraft scatter studies, path characterization. Moderate — carrier
tracking (#78) applied to a reflected path.

### 85. ML modulation classifier 🔴
Train a small model to label signals live (AM/SSB/CW/FSK/PSK/FT8…). HF is a
free, messy, real-world dataset.
Use cases: hands-free band ID (#37), research. Ambitious — data collection,
labeling, model, inference in the flowgraph.

### 86. ML denoiser / weak-signal enhancer 🔴
Train on captured SigMF to denoise or pull weak signals out of noise; evaluate
against classical methods.
Use cases: weak-signal work, research. Ambitious.

### 87. Cyclostationary feature detector 🟡
Detect signals below the noise floor by their cyclostationary signatures rather
than raw energy — finds modulated signals a power detector misses.
Use cases: weak-signal detection, spectrum sensing. Moderate-to-hard.

### 88. Automatic notch tracker 🟡
Detect and null steady carriers/heterodynes automatically, following them if they
drift.
Use cases: cleaning up QRM under SSB/CW. Moderate — peak track + adaptive notch.

---

## I. Educational / demo

### 89. Live constellation / eye / time scope 🟢
Use `qtgui`'s constellation, eye, and time-domain displays on a real HF signal —
see modulation as it actually is.
Use cases: teaching, debugging. Easy — the sinks exist.

### 90. "What SSB/CW/FT8 looks like in IQ" 🟢
A guided display that shows the same signal in spectrum, time, and constellation
so a newcomer *sees* the difference between modes.
Use cases: club talks, self-education. Easy — presets on #89.

### 91. Watterson HF-channel-model validation 🟡
Capture a real fading signal and compare its statistics against the simulated HF
channel in `rf-bench .../educational/iq/hf-static.py` — does the model match
reality?
Use cases: validating the simulator, teaching fading. Moderate — statistics +
comparison.

### 92. Interactive filter-design demo 🟢
Drag the passband and hear/see the effect in real time — the "filters, made
tangible" demo.
Use cases: teaching DSP. Easy — #20 with a teaching UI.

### 93. Modulation zoo 🟡
A side-by-side gallery of every mode captured off-air, each with its spectrum and
a short audio/decoded sample.
Use cases: reference, teaching, "what does that sound like?" Moderate — curated
captures + display.

### 94. IQ-imbalance / mirror-image demo 🟢
Deliberately flip or imbalance I/Q and show the sideband swap / mirror images —
makes an abstract concept obvious.
Use cases: teaching why Q-first vs I-first matters (a real solsdr gotcha!). Easy.

---

## J. Infrastructure & glue (make everything above easier)

### 95. Reusable `SolsdrSource` OOT block 🟢
Package `_SolsdrTCPSource` (plus header parsing and reconnect) as a proper GNU
Radio out-of-tree block so any flowgraph — including GRC-built ones — can drop in
a solsdr source. This unlocks everything else.
Use cases: every other idea; GRC usability. Easy-ish and high-leverage.

### 96. GRC example flowgraphs 🟢
Ship `.grc` files so users build visually in GNU Radio Companion instead of
hand-writing Python.
Use cases: approachability, teaching. Easy once #95 exists.

### 97. SigMF recorder + player 🟢
Record IQ to SigMF with correct metadata (rate/freq/timestamp/GPSDO ref) and play
it back into flowgraphs — the foundation for offline work and dataset building.
Use cases: forensics, ML datasets, replaying band openings. Easy.

### 98. Auto-reconnect source block 🟡
Survive solsdr restarts/retunes without tearing down the flowgraph — reconnect
and resume cleanly.
Use cases: unattended/long-running monitors. Moderate — state machine in the source.

### 99. Retune-follow 🟡
Have the source re-read the header when solsdr changes frequency so the display's
center/labels update automatically.
Use cases: keep the panadapter honest as you tune the radio. Moderate — header
re-read protocol or a side channel.

### 100. Headless "appliance" mode 🟡
A flowgraph + config file with no GUI, meant to run on a Pi as a fixed-function
sensor (occupancy logger, beacon monitor, WSPR beacon) — fits solsdr's headless
design philosophy.
Use cases: dedicated always-on monitors. Moderate — config-driven flowgraph +
service packaging.

---

## Honest constraints

- **Bandwidth ceiling: ~150 kHz usable.** Fine for one band or a chunk of it;
  you can't watch all of HF at once (that's a wideband-SDR job).
- **Low IQ amplitude.** solsdr IQ peaks around −90 dBFS — the `--gain` lesson
  from the waterfall applies everywhere; anything measuring absolute levels
  needs the same calibration care as the TX-power work.
- **It's a network client.** GR pulls over TCP; 312.5 kHz complex64 is
  ~5 Mbit/s — trivial on LAN, fine over WireGuard.
- **RX2** ideas need the second IQ stream wired (enable is RE'd — `ARTEMISSDR.md`
  byte 54 — but consuming its stream is unbuilt).
- **TX** ideas need a TX-IQ intake into solsdr and go through the same interlocks
  + per-band calibration as all transmit: **dummy load + wattmeter first.**

## Existing pieces to build on

- `qt_iq_waterfall.py` — combined FFT + waterfall; reference for connecting,
  header parsing, and the `_SolsdrTCPSource` block.
- `../iq_client.py` + `../README.md` — raw client library + GNU Radio notes.
