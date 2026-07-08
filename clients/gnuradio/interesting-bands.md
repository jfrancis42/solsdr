# Most interesting 150 kHz of HF to cover

solsdr captures up to ~150 kHz of contiguous spectrum at once (312.5 kHz IQ,
~150 kHz usable). This ranks the best single 150 kHz slices to point it at, for
a mix of decode density and sheer interest — **a daytime top 5, then a
nighttime top 5**, since HF propagation flips completely between them.

**Daytime drives the answer.** In daylight, D-layer absorption guts the bands
below ~7 MHz for anything but local/regional work, while 14 MHz and up are wide
open. So the best daytime picks cluster at 8 MHz and above. This ranking also
weights toward *this* setup: because solsdr feeds a multi-decoder / wide-capture
toolkit, signal **density and decodability** matter as much as raw intrigue.

> ⚠️ Frequencies below are from memory and approximate. Exact segment edges vary
> by ITU region and change over time — **verify against a current band plan**
> before relying on them. All amateur digital/CW frequencies are USB/CW as noted;
> utility voice is USB.

---

# Daytime top 5

In daylight, D-layer absorption guts the bands below ~7 MHz for anything but
local/regional work, while 14 MHz and up are wide open — so the daytime picks
cluster at 8 MHz and above.

## 1. 20 m digital + CW — 14.000–14.150 MHz  🥇

The clear winner *for this setup*. One 150 kHz grab captures the CW segment
(≈14.000–14.070) **plus every digital watering hole simultaneously**: FT8
(14.074), JS8 (14.078), FT4 (14.080), WSPR (≈14.0956), and the RTTY/PSK clusters.

This is exactly what the "N decoders from one capture" toolkit was built for — a
single capture yields hundreds of FT8 decodes, dozens of simultaneous CW QSOs,
plus RTTY and PSK, all cross-referenced against the `govt-data` callsign API for
name/QTH/grid. 20 m is *the* daytime DX band: reliably open worldwide in
daylight and essentially always alive. Nothing else offers this decode density.

**Best exploited by:** whole-band FT8/FT4/JS8 multi-decode, the CW skimmer,
callsign enrichment, occupancy logging.

## 2. Oceanic aeronautical (8 MHz) — 8815–8965 kHz  🥈

If the metric is "most fascinating *content*," this arguably beats #1. It's the
transoceanic air-traffic-control band (SSB/USB voice): Gander, Shanwick, New
York, Santa Maria working airliners over the Atlantic with position reports,
plus SELCAL data bursts you can decode to identify specific aircraft.

Real-time, operational, and genuinely captivating to follow. 8 MHz propagates
well over medium/long haul during the day. Lower density than 20 m (long quiet
stretches punctuated by bursts), so it rewards a triggered recorder + occasional
monitoring more than a decoder farm.

**Best exploited by:** SSB voice monitoring, SELCAL decode, the triggered SigMF
recorder, the "new signal" alerter.

## 3. HFGCS + upper aero — 11175–11325 kHz  🥉

11175 kHz USB is the USAF **High Frequency Global Communications System** — the
iconic phonetic **EAM broadcasts** ("Skyking, Skyking, do not answer…"). Around
it sits more oceanic aero (≈11279, 11336 NAT families).

Highest "wow" factor of the list. 11 MHz is a solid daytime band. The catch: it's
sparse across 150 kHz — you're mostly waiting on 11175 for activity, so it
doesn't exploit the wide capture the way 20 m does. Best paired with the
"new signal" alerter so it pings you (SMS via `money/sms.py`) when an EAM starts.

**Best exploited by:** narrowband USB monitoring + energy-triggered alerting/
recording.

## 4. Daytime shortwave broadcast — 19 m, ~15100–15250 kHz

The daytime international broadcast workhorse. A 150 kHz slice holds many
concurrent AM broadcasters from around the globe — excellent for the panadapter,
occupancy logging, and propagation study (which exotic stations fade in and out
makes a great heatmap on the `maps` server).

Content is "just" broadcast — less operational intrigue than the aero/military
picks — but the density of strong, identifiable AM carriers makes it the best
band for the *measurement/monitoring* side of the idea list.

**Best exploited by:** band-occupancy logging → MQTT → Grafana, propagation
heatmap, synchronous-AM demod, waterfall timelapse.

## 5. Maritime HF (8 MHz marine) — ~8400–8550 kHz

DSC data bursts (8414.5 kHz), ship/coast SSB, and weatherfax nearby. It's been
declining since satellite took over most maritime traffic, so it's a niche pick
— but the DSC and WEFAX give you *decodable data* (positions, distress alerts,
weather charts), a genuinely different flavor from everything above. Daytime
8 MHz propagation is fine for coastal/medium range.

**Best exploited by:** DSC decode, WEFAX → image, SSB monitoring.

---

# Nighttime top 5

After dark the D-layer vanishes, so the **low bands come alive** (40/80/160 m go
long — worldwide DX where in daytime they were local-only) while the high bands
(20 m and up) fade out as the F-layer can no longer support them without sunlight.
The night picks therefore cluster **below ~10 MHz**, and the flavor shifts from
aero/broadcast toward amateur DX, clandestine/pirate broadcasting, and the eerie
utility/number-station world that prefers the cover of darkness.

## 1. 40 m digital + CW — 7.000–7.150 MHz  🥇

The nighttime counterpart to daytime 20 m, and the best all-round night pick for
this setup. One 150 kHz slice grabs the CW segment (≈7.000–7.040) plus FT8
(7.074), FT4 (7.047/7.080 region), JS8 (7.078), WSPR (≈7.0386), and RTTY/PSK
clusters. 40 m is *the* reliable nighttime DX band — it goes long after dark and
stays busy all night, so the multi-decoder rig runs hot: hundreds of FT8 decodes,
a full CW skimmer's worth of QSOs, all enriched via the `govt-data` API.

Region note: 7.100–7.200 is also broadcast in some ITU regions, so the exact
150 kHz you center matters — the 7.000–7.150 slice keeps you in the dense
amateur data/CW zone worldwide.

**Best exploited by:** whole-band FT8/FT4/JS8 multi-decode, CW skimmer, callsign
enrichment, occupancy logging.

## 2. 80/75 m — 3.500–3.650 or 3.750–3.900 MHz  🥈

The classic nighttime ragchew and net band, and after dark it opens to
continental/regional DX. Two flavors depending on where you center: the low end
(≈3.500–3.650) is CW + FT8 (3.573) + digital, dense with decodable content; the
high end (≈3.750–3.900) is SSB — regional nets, ragchews, emergency/traffic nets
that run late. Great for "who's talking on the band at 0200 local" and for
occupancy/propagation logging that shows the band lengthening through the night.

**Best exploited by:** SSB/CW monitoring, net logging, FT8 decode (low end),
occupancy trending.

## 3. Tropical-band & clandestine broadcast — 4.750–4.900 or ~4.940–5.060 MHz  🥉

The 60 m broadcast region (the "tropical bands") comes into its own at night:
low-power regional broadcasters from Africa, South America, and Asia that are
inaudible by day, plus this is prime territory for **clandestine and pirate
stations** that favor darkness. Highest "exotic catch" factor after dark — chasing
a 1 kW station in the Andes fading up at your sunrise is the SWL equivalent of DX.
Sparser and weaker than the amateur bands, so it rewards the panadapter +
patient monitoring + a good propagation heatmap more than a decoder farm.

**Best exploited by:** panadapter + waterfall, synchronous-AM demod (fady weak
carriers), occupancy/propagation logging, triggered recorder.

## 4. Number stations & HF utility — ~4.600–4.800 MHz (and the 5–6 MHz utility pockets)

The nighttime home of the genuinely mysterious: **number stations** (automated
voice/CW/digital broadcasts of coded groups — still active), along with military
and diplomatic utility traffic (STANAG/MIL-STD data bursts, ALE handshakes) that
concentrates in the darkness. Nothing decodes to plain meaning, which is exactly
the appeal — the "what *is* that?" band. Activity is intermittent across 150 kHz,
so pair it with the "new signal" alerter and the triggered SigMF recorder so
bursts get captured whether or not you're watching. ALE and STANAG bursts are
themselves partly decodable (you have an ALE project in `~/ale/` to bridge to).

**Best exploited by:** energy-triggered recording + alerting, ALE/STANAG burst
capture, automatic mode-ID, long-baseline recording for offline analysis.

## 5. 30 m — 10.100–10.150 MHz

The quiet, always-interesting middle band. It's narrow (only 50 kHz of amateur
allocation, so a 150 kHz capture also scoops adjacent utility/WWV territory), CW-
and digital-only by band plan — no phone — so it's dense with FT8 (10.136), CW,
and WSPR (≈10.1387) and almost never crowded. 30 m is a genuine day/night
crossover band that often stays open when both 40 and 20 are marginal, making it
a reliable "something is always workable" pick late at night. Bonus: WWV's
10 MHz standard carrier sits just below, so the same capture supports the
**GPSDO frequency-error monitor** (idea #46/#79) alongside the amateur decoding.

**Best exploited by:** FT8/CW/WSPR decode, WWV frequency monitoring (GPSDO),
occupancy logging — a low-effort always-on nighttime monitor.

---

## Recommendation

**Day:** start on daytime **#1 (20 m, 14.000–14.150)** — the only choice that
fully lights up the multi-decoder rig, and productive in daylight essentially
always. For *listening* intrigue over decode volume, retune to daytime **#2
(oceanic aero)** — the one that makes people lean in: "wait, that's a plane over
the middle of the Atlantic, right now."

**Night:** start on nighttime **#1 (40 m, 7.000–7.150)** for the same reason 20 m
wins by day — maximum decodable density on the reliable after-dark DX band. For
intrigue, swing to nighttime **#4 (number stations / HF utility)**, the "what
*is* that?" band that only makes sense in the dark.

**A nice always-on pair:** run daytime 20 m and nighttime 40 m on a
sun-elevation schedule and you have a decoder station that's productive around
the clock; nighttime **#5 (30 m)** is the graceful crossover when neither is
clearly better (and it doubles as the WWV/GPSDO frequency monitor).

## Caveat: solar cycle & time of day

All of this shifts with the solar cycle. Near solar **maximum**, the day picks
push higher — add **10 m (28.000–28.150 MHz)**, a worldwide daytime band that
packs FT8/CW/beacons densely — and even the night bands stay higher longer. Near
solar **minimum**, lean lower both day and night (the utility picks and 40/80 m
hold up better than the upper HF amateur bands). The day/night flip is driven by
D-layer absorption (present only in sunlight) and F-layer support (needs sunlight
to hold the high bands up); dawn/dusk grey-line periods briefly favor the low
bands for long-path DX. Latitude, season, and current SFI/K-index sharpen every
choice — give me those and I can tighten the picks.
