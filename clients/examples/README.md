# Example IQ captures

Recorded solsdr IQ streams for offline demos — chiefly the panadapter's
`--file` replay mode, so you can see it work with no radio attached.

The short **demo clip is committed** so the panadapter has a zero-setup example.
The **full-length master is git-ignored** (the 300 s capture is ~94 MB; GitHub
rejects files over 100 MB and any large binary bloats every clone forever) — it
lives only in the local tree; regenerate it from a radio with the capture tool.

## Files

| File | Freq | Rate | Duration | In repo? | Notes |
|------|------|------|----------|----------|-------|
| `solsdr_20m_demo30.iq` | 14074 kHz (20 m FT8) | 39062.5 S/s | 30 s | ✅ committed (~9.4 MB) | Zero-setup panadapter demo. Trimmed from the master. |
| `solsdr_20m_example.iq` | 14074 kHz (20 m FT8) | 39062.5 S/s | 300 s | ✗ git-ignored (~94 MB) | Full busy-FT8 master; regenerate locally. |

## Format

Each file is solsdr's on-the-wire IQ format: one text header line, then raw
little-endian interleaved float32 I,Q (numpy `complex64`):

```
SOLSDR IQ rate=39062.5 fmt=complex64 freq=14074000\n<complex64 samples...>
```

Because it's byte-for-byte what the IQ server emits, it self-describes (rate +
center freq) — no sidecar metadata needed.

## Replay it (panadapter demo)

```bash
python3 clients/panadapter.py --file clients/examples/solsdr_20m_demo30.iq
```

It loops at EOF so the demo runs indefinitely; add `--no-loop` to stop at the
end. A headerless raw complex64 file also works with `--file-rate`/`--file-freq`.

## Regenerate

On the host wired to the radio:

```bash
python3 tools/capture_iq_stream.py 14074 --seconds 300 \
    --out clients/examples/solsdr_20m_example.iq
```

## Make a short clip (for committing / a quick demo)

Trim the master to N seconds without a radio — just copies the header + the
first `rate*N` samples:

```bash
python3 tools/trim_iq.py clients/examples/solsdr_20m_example.iq \
    clients/examples/solsdr_20m_demo30.iq --seconds 30      # ~9.4 MB
```
