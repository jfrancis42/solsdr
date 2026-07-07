import sys, struct, time
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from solsdr.protocol import packet as pk
from solsdr.protocol.rx_stream import RXStreamReceiver

# Build a synthetic RX packet (Q=i, I=-i) like the existing test
header = bytearray(10)
header[0]=0x01; header[1]=0xFF; header[2]=0xFE
header[4:6]=struct.pack('<H',1200)
payload = bytearray(1200)
for i in range(200):
    off=i*6
    payload[off:off+3]=(i).to_bytes(3,'little',signed=True)       # Q
    payload[off+3:off+6]=(-i).to_bytes(3,'little',signed=True)    # I
packet = bytes(header)+bytes(payload)

# New vectorized decoder
new = pk.decode_iq_packet(packet)
# Old loop decoder
old = RXStreamReceiver(callback=lambda x: None)._decode_packet(packet)

assert new is not None and old is not None
assert np.allclose(new, old, atol=1e-7), "MISMATCH old vs new decoder"
print("✓ vectorized decode matches old decoder exactly")

# Verify semantics: sample i should be I=-i/2^23 + jQ=i/2^23
for i in range(5):
    assert abs(new[i].real - (-i/8388608.0)) < 1e-7
    assert abs(new[i].imag - (i/8388608.0)) < 1e-7
print("✓ decode semantics correct (I=real, Q=imag, Q-first on wire)")

# TX round-trip: encode complex -> decode back
rng = np.random.default_rng(42)
iq = (rng.uniform(-0.9,0.9,200) + 1j*rng.uniform(-0.9,0.9,200)).astype(np.complex64)
enc = pk.encode_iq_packet(iq, seq=7)
assert len(enc)==1210, f"TX packet wrong size {len(enc)}"
assert enc[2]==0xFD, "TX opcode wrong"
assert enc[6]|(enc[7]<<8)==7, "seq not encoded"
# Decode it back (fix opcode to RX for decoder, keep payload)
rx_equiv = bytes([0x01,0xFF,0xFE]) + enc[3:]
back = pk.decode_iq_packet(rx_equiv)
# 24-bit quantization error tolerance
assert np.max(np.abs(back-iq)) < 2/8388608.0, f"TX roundtrip err {np.max(np.abs(back-iq))}"
print("✓ TX encode round-trips within 24-bit quantization")

# Discovery probe matches ExpertSDR3 captured bytes
assert pk.build_discovery_probe(0x01).hex()=='01ff001a000000000000000000000000000000000000fde6'
assert pk.build_discovery_probe(0x32).hex()=='32ff001a000000000000000000000000000000000000cce6'
print("✓ discovery probe bytes match captured ExpertSDR3 probes")

# Perf: new vs old on 2000 packets
N=2000
t=time.perf_counter()
for _ in range(N): pk.decode_iq_packet(packet)
tnew=time.perf_counter()-t
rx=RXStreamReceiver(callback=lambda x:None)
t=time.perf_counter()
for _ in range(N): rx._decode_packet(packet)
told=time.perf_counter()-t
print(f"\nperf over {N} packets: old={told*1000:.0f}ms  new={tnew*1000:.0f}ms  speedup={told/tnew:.1f}x")
print(f"new decoder: {N/tnew:.0f} pkt/s (need ~1562/s)  headroom={N/tnew/1562:.0f}x")
print("\nALL CODEC TESTS PASSED")
