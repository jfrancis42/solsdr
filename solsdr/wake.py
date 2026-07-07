#!/usr/bin/env python3
'''
SunSDR2 wake + discovery.

Combines two wake mechanisms so it works whether the radio's NIC is merely
idle (answers broadcast discovery) or fully asleep at layer 2 (needs a
Wake-on-LAN magic packet first):

  1. Wake-on-LAN magic packet to the radio's MAC (broadcast).
  2. SunSDR broadcast discovery probe (XX ff 00 1a + checksum) to
     255.255.255.255:50001 and the directed subnet broadcast.

Loops both until the radio replies with its discovery packet (XX ff 01 1a),
from which we extract the radio IP and control port.

Discovery probe format extracted verbatim from ArtemisSDR
clsSunSDRDiscovery.cs (buildQueryPacket / Probe).
'''
import socket
import struct
import time

# Family selector byte (packet[0]). 0x01=PRO, 0x32=DX; others probed for
# older variants, matching ExpertSDR3's multi-family probe.
FAMILY_BYTES = [0x01, 0x32, 0x42, 0x22, 0x12, 0x03]
CONTROL_PORT = 50001
# Radio MAC observed in ARP reply during cold_start.pcap (2026-07-06).
DEFAULT_MAC = '00:ee:00:00:00:7d'


def build_discovery_probe(family):
    '''24-byte SunSDR discovery probe: XX ff 00 1a + one's-complement cksum.'''
    pkt = bytearray(24)
    pkt[0] = family
    pkt[1] = 0xff
    pkt[2] = 0x00
    pkt[3] = 0x1a
    s = 0
    for i in range(0, 22, 2):
        s += pkt[i] | (pkt[i + 1] << 8)
        if s & 0x10000:
            s = (s & 0xffff) + 1
    ck = (~s) & 0xffff
    pkt[22] = ck & 0xff
    pkt[23] = (ck >> 8) & 0xff
    return bytes(pkt)


def build_wol(mac):
    '''Standard Wake-on-LAN magic packet: 6x0xFF then MAC repeated 16 times.'''
    mac_bytes = bytes(int(x, 16) for x in mac.split(':'))
    return b'\xff' * 6 + mac_bytes * 16


def is_discovery_reply(buf):
    return len(buf) >= 24 and buf[1] == 0xff and buf[2] == 0x01 and buf[3] == 0x1a


def parse_reply(buf):
    # IP is big-endian at offset 10 (per captured reply 0a010203 = 10.1.2.3);
    # control port little-endian at offset 18.
    ip = '.'.join(str(b) for b in buf[10:14])
    port = buf[18] | (buf[19] << 8)
    if port == 0:
        port = CONTROL_PORT
    return ip, port


def wake(local_ip='10.1.2.185', bcast='10.1.2.255', mac=DEFAULT_MAC,
         timeout=120, verbose=True):
    '''Fire WoL + discovery until the radio answers. Returns (ip, port) or None.'''
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((local_ip, 0))
    sock.settimeout(1.0)

    wol = build_wol(mac)
    probes = [build_discovery_probe(f) for f in FAMILY_BYTES]
    dests = [(bcast, CONTROL_PORT), ('255.255.255.255', CONTROL_PORT)]

    start = time.time()
    rnd = 0
    while time.time() - start < timeout:
        rnd += 1
        # WoL magic packet to discovery + WoL ports
        for wport in (9, 7, CONTROL_PORT):
            sock.sendto(wol, (bcast, wport))
            sock.sendto(wol, ('255.255.255.255', wport))
        # Discovery probes
        for dest in dests:
            for p in probes:
                sock.sendto(p, dest)
        if verbose:
            print(f'round {rnd}: sent WoL + {len(probes)} probes x {len(dests)} dests')
        # Listen ~2s
        end = time.time() + 2
        while time.time() < end:
            try:
                resp, addr = sock.recvfrom(1500)
            except socket.timeout:
                break
            if is_discovery_reply(resp):
                ip, port = parse_reply(resp)
                if verbose:
                    print(f'\n*** RADIO AWAKE: reply from {addr}')
                    print(f'    hex={resp[:24].hex()}  ip={ip} ctrl_port={port}')
                sock.close()
                return ip, port
    sock.close()
    return None


if __name__ == '__main__':
    import sys
    t = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    print(f'Waking SunSDR2 (WoL MAC {DEFAULT_MAC} + broadcast discovery), timeout {t}s...')
    result = wake(timeout=t)
    if result:
        print(f'\nSUCCESS: radio at {result[0]}:{result[1]}')
        sys.exit(0)
    else:
        print('\nNo response. Radio NIC may be fully powered down (master switch off).')
        sys.exit(1)
