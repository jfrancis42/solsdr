# Clients

The SunSDR2 SDR streams raw complex64 IQ over TCP so external tools consume it
as network clients. Start the server side with:

```bash
python3 solsdr_receiver.py 14074 --iq-server        # IQ on tcp :5555
```

On connect the server sends one text line, then a continuous stream of
little-endian interleaved float32 I,Q pairs (numpy `complex64.tobytes()`):

```
SOLSDR IQ rate=39062.5 fmt=complex64 freq=14074000\n
<complex64 samples...>
```

## Python client

```bash
python3 clients/iq_client.py 10.1.2.185 5555 --seconds 8
```

## GNU Radio

Use a **TCP Source** (or *Socket PDU* / *TCP Client Source* depending on GR
version) configured for:

- Address: the radio host, port 5555
- Type: **Complex Float 32** (`gr_complex`)
- The first line is a text header — either consume/skip the first ~48 bytes up
  to the newline in a small preprocessing block, or (simplest) point a
  `File Source`/`TCP Source` at the stream and drop the first partial sample.

Sample rate for downstream blocks: **39062.5 Hz** (the PRO's native IQ rate).
Note this is the PRO value; a DX would advertise its own rate in the header —
always read `rate=` from the header rather than hard-coding it, so the same
flowgraph works when DX support lands.

A minimal flowgraph:

```
TCP Source (complex, host:5555)
  -> Frequency Xlating FIR Filter (select/shift the signal of interest)
  -> resampler to audio rate
  -> demod (WBFM / AM / SSB via complex-to-real + Hilbert)
  -> Audio Sink
```

Because the server just publishes raw IQ, multiple clients (GNU Radio + a
recorder + a decoder) can connect simultaneously and each do their own DSP.
