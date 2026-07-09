#!/usr/bin/env python3
"""
Trim a wire-format IQ capture to the first N seconds.

The capture format is a 'SOLSDR IQ ...' header line then raw complex64, so a
trim is just: copy the header, then copy rate*seconds*8 bytes of samples. No
radio, no re-capture. Handy for cutting a big recording down to a small demo
clip small enough to commit to GitHub.

Usage:
    python3 tools/trim_iq.py in.iq out.iq --seconds 30
"""
import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser(description="trim a wire-format IQ capture")
    ap.add_argument("infile")
    ap.add_argument("outfile")
    ap.add_argument("--seconds", type=float, default=30.0,
                    help="keep the first N seconds (default 30)")
    args = ap.parse_args()

    with open(args.infile, "rb") as f:
        head = f.read(256)
        if not head.startswith(b"SOLSDR"):
            sys.exit("input is not a SOLSDR wire-format IQ file (no header). "
                     "For headerless raw complex64, use dd/head with a byte count.")
        nl = head.find(b"\n")
        header = head[:nl + 1]
        line = header.decode("ascii", "replace").strip()
        rate = 39062.5
        for tok in line.split():
            if tok.startswith("rate="):
                rate = float(tok.split("=", 1)[1])
        data_off = nl + 1

        keep_samples = int(rate * args.seconds)
        keep_bytes = keep_samples * 8            # complex64 = 8 bytes/sample

        f.seek(data_off)
        with open(args.outfile, "wb") as out:
            out.write(header)
            remaining = keep_bytes
            wrote = 0
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                wrote += len(chunk)
                remaining -= len(chunk)

    got_s = wrote / 8 / rate if rate else 0
    print(f"{line}")
    print(f"wrote {wrote/1e6:.1f} MB ({wrote//8} samples, {got_s:.1f}s) "
          f"-> {args.outfile}")
    if wrote < keep_bytes:
        print(f"note: input was shorter than {args.seconds:.0f}s; kept all of it.")


if __name__ == "__main__":
    main()
