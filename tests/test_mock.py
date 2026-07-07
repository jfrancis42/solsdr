import sys, socket, struct, time, threading
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from solsdr.mock_radio import MockRadio, CONTROL_PORT, RX_STREAM_PORT
from solsdr.protocol import packet as pk

# Bind RX socket FIRST so we don't miss stream packets
rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
rx.bind(('127.0.0.1', RX_STREAM_PORT))
rx.settimeout(2)

radio = MockRadio(bind_ip='127.0.0.1', client_ip='127.0.0.1', tone_hz=1000.0, verbose=True)
radio.start()
time.sleep(0.3)

c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
c.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
c.settimeout(2)

# 1. Discovery
c.sendto(pk.build_discovery_probe(0x01), ('127.0.0.1', CONTROL_PORT))
reply,_ = c.recvfrom(1024)
parsed = pk.parse_discovery_reply(reply)
assert parsed == ('10.1.2.3', 50001), f"discovery parse {parsed}"
print("✓ discovery: radio replied", parsed)

# 2. Control ACK (keepalive)
c.sendto(pk.build_control_packet(0x18, b'\x00'*4), ('127.0.0.1', CONTROL_PORT))
ack,_ = c.recvfrom(1024)
assert ack[2]==0x18, "keepalive ack opcode"
print("✓ keepalive ACKed")

# 3. STATE_SYNC + freq -> should start stream
c.sendto(pk.build_control_packet(0x01, b'\x00'*40), ('127.0.0.1', CONTROL_PORT))
c.recvfrom(1024)
freqpl = b'\x00'*8 + struct.pack('<Q', 10_000_000*10)
c.sendto(pk.build_control_packet(0x09, freqpl), ('127.0.0.1', CONTROL_PORT))
c.recvfrom(1024)
assert radio.freq_hz == 10_000_000, f"freq {radio.freq_hz}"
print("✓ freq set to", radio.freq_hz)

# 4. Collect ~200 stream packets, decode, verify tone
samples = []
count = 0
t0 = time.perf_counter()
while count < 300:
    try:
        pkt,_ = rx.recvfrom(2048)
    except socket.timeout:
        break
    iq = pk.decode_iq_packet(pkt)
    if iq is not None:
        samples.append(iq); count += 1
elapsed = time.perf_counter()-t0
assert count >= 200, f"only {count} pkts"
iq = np.concatenate(samples)
# FFT to find tone
fft = np.fft.fftshift(np.fft.fft(iq[:8192]))
freqs = np.fft.fftshift(np.fft.fftfreq(8192, 1/312500))
peak = freqs[np.argmax(np.abs(fft))]
print(f"✓ received {count} IQ pkts in {elapsed*1000:.0f}ms ({count/elapsed:.0f} pkt/s, need ~1562)")
print(f"✓ tone detected at {peak:.0f} Hz (expected ~1000 Hz)")
assert abs(peak-1000) < 50, f"tone off: {peak}"

radio.stop()
rx.close(); c.close()
print("\nMOCK RADIO END-TO-END TEST PASSED")
