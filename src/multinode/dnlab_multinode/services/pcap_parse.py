"""Minimal pure-Python pcap parser for Follow the Rabbit.

Captures arrive as raw pcap bytes on the orchestrator (``tcpdump -w -``). The
PCAP stays strictly backend: it is parsed here and never exposed to the user
(no PCAP download, no raw payload in the GUI). Only the few binary fields needed
to build a ``PacketObservation`` are extracted — TTL/hop-limit + the instance
key + the 5-tuple — because those binary fields are robust and version-stable,
unlike textual ``tcpdump -v`` output. No new dependency is introduced.

Supported: pcap (µs and ns) and the classic byte orders, Ethernet (incl. one
802.1Q tag), IPv4 / IPv6 (one extension header skipped), ICMP / ICMPv6 / TCP /
UDP. Anything unrecognized is skipped rather than raising.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Link-layer / ethertypes / IP protocol numbers.
_LINKTYPE_ETHERNET = 1
_LINKTYPE_RAW = 101          # raw IP (no L2), DLT_RAW
_ETH_P_IP = 0x0800
_ETH_P_IPV6 = 0x86DD
_ETH_P_8021Q = 0x8100
_IPPROTO_ICMP = 1
_IPPROTO_TCP = 6
_IPPROTO_UDP = 17
_IPPROTO_ICMPV6 = 58
# IPv6 extension headers we transparently skip to reach the L4 header.
_IPV6_EXT_HEADERS = {0, 43, 44, 60}

_PCAP_MAGIC_USEC = 0xA1B2C3D4
_PCAP_MAGIC_NSEC = 0xA1B23C4D


@dataclass(frozen=True)
class ParsedPacket:
    ts: float
    proto: str               # "icmp" | "icmp6" | "tcp" | "udp" | ""
    src_ip: str
    dst_ip: str
    ttl: int                 # IPv4 TTL or IPv6 hop limit
    ip_id: int               # IPv4 identification (0 for IPv6)
    src_port: int = 0
    dst_port: int = 0
    icmp_id: int | None = None
    icmp_seq: int | None = None
    tcp_seq: int | None = None
    tcp_flags: int | None = None
    payload_len: int = 0


class IncrementalPcapParser:
    """Incrementally parse a pcap byte stream.

    ``tcpdump -w - -U`` writes the global header once and then packet records.
    SSH stdout can split those bytes anywhere, so this parser buffers partial
    headers/records and returns packets only when a full record is available.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._endian = ""
        self._nanos = False
        self._linktype = 0
        self._ready = False
        self._invalid = False

    def feed(self, data: bytes) -> list[ParsedPacket]:
        if self._invalid or not data:
            return []
        self._buf.extend(data)
        if not self._ready:
            if len(self._buf) < 24:
                return []
            magic = struct.unpack("<I", self._buf[:4])[0]
            if magic in (_PCAP_MAGIC_USEC, _PCAP_MAGIC_NSEC):
                self._endian = "<"
                self._nanos = magic == _PCAP_MAGIC_NSEC
            else:
                magic_be = struct.unpack(">I", self._buf[:4])[0]
                if magic_be not in (_PCAP_MAGIC_USEC, _PCAP_MAGIC_NSEC):
                    self._invalid = True
                    self._buf.clear()
                    return []
                self._endian = ">"
                self._nanos = magic_be == _PCAP_MAGIC_NSEC
            self._linktype = struct.unpack(self._endian + "I", self._buf[20:24])[0]
            del self._buf[:24]
            self._ready = True

        packets: list[ParsedPacket] = []
        while len(self._buf) >= 16:
            ts_sec, ts_frac, caplen, _origlen = struct.unpack(self._endian + "IIII", self._buf[:16])
            if len(self._buf) < 16 + caplen:
                break
            frame = bytes(self._buf[16:16 + caplen])
            del self._buf[:16 + caplen]
            ts = ts_sec + ts_frac / (1_000_000_000 if self._nanos else 1_000_000)
            pkt = _parse_frame(frame, self._linktype, ts)
            if pkt is not None:
                packets.append(pkt)
        return packets


def parse_pcap(data: bytes) -> list[ParsedPacket]:
    """Parse pcap bytes into the L3/L4 fields Follow the Rabbit needs."""
    if len(data) < 24:
        return []
    magic = struct.unpack("<I", data[:4])[0]
    if magic in (_PCAP_MAGIC_USEC, _PCAP_MAGIC_NSEC):
        endian = "<"
        nanos = magic == _PCAP_MAGIC_NSEC
    else:
        magic_be = struct.unpack(">I", data[:4])[0]
        if magic_be not in (_PCAP_MAGIC_USEC, _PCAP_MAGIC_NSEC):
            return []
        endian = ">"
        nanos = magic_be == _PCAP_MAGIC_NSEC
    linktype = struct.unpack(endian + "I", data[20:24])[0]

    packets: list[ParsedPacket] = []
    offset = 24
    n = len(data)
    while offset + 16 <= n:
        ts_sec, ts_frac, caplen, _origlen = struct.unpack(endian + "IIII", data[offset:offset + 16])
        offset += 16
        if offset + caplen > n:
            break
        frame = data[offset:offset + caplen]
        offset += caplen
        ts = ts_sec + ts_frac / (1_000_000_000 if nanos else 1_000_000)
        pkt = _parse_frame(frame, linktype, ts)
        if pkt is not None:
            packets.append(pkt)
    return packets


def _parse_frame(frame: bytes, linktype: int, ts: float) -> ParsedPacket | None:
    if linktype == _LINKTYPE_ETHERNET:
        if len(frame) < 14:
            return None
        ethertype = struct.unpack(">H", frame[12:14])[0]
        payload = frame[14:]
        if ethertype == _ETH_P_8021Q:
            if len(payload) < 4:
                return None
            ethertype = struct.unpack(">H", payload[2:4])[0]
            payload = payload[4:]
    elif linktype == _LINKTYPE_RAW:
        if not frame:
            return None
        version = frame[0] >> 4
        ethertype = _ETH_P_IP if version == 4 else _ETH_P_IPV6
        payload = frame
    else:
        return None

    if ethertype == _ETH_P_IP:
        return _parse_ipv4(payload, ts)
    if ethertype == _ETH_P_IPV6:
        return _parse_ipv6(payload, ts)
    return None


def _parse_ipv4(data: bytes, ts: float) -> ParsedPacket | None:
    if len(data) < 20:
        return None
    ihl = (data[0] & 0x0F) * 4
    if ihl < 20 or len(data) < ihl:
        return None
    total_len = struct.unpack(">H", data[2:4])[0]
    ip_id = struct.unpack(">H", data[4:6])[0]
    ttl = data[8]
    proto_num = data[9]
    src_ip = _ipv4_addr(data[12:16])
    dst_ip = _ipv4_addr(data[16:20])
    l4 = data[ihl:]
    payload_len = max(0, total_len - ihl)
    return _parse_l4(proto_num, l4, ts, src_ip, dst_ip, ttl, ip_id, payload_len)


def _parse_ipv6(data: bytes, ts: float) -> ParsedPacket | None:
    if len(data) < 40:
        return None
    payload_len = struct.unpack(">H", data[4:6])[0]
    next_header = data[6]
    hop_limit = data[7]
    src_ip = _ipv6_addr(data[8:24])
    dst_ip = _ipv6_addr(data[24:40])
    offset = 40
    # Skip extension headers to reach the L4 header (best effort).
    guard = 0
    while next_header in _IPV6_EXT_HEADERS and offset + 2 <= len(data) and guard < 8:
        ext_len = (data[offset + 1] + 1) * 8
        next_header = data[offset]
        offset += ext_len
        guard += 1
    l4 = data[offset:]
    return _parse_l4(next_header, l4, ts, src_ip, dst_ip, hop_limit, 0, max(0, payload_len))


def _parse_l4(
    proto_num: int,
    l4: bytes,
    ts: float,
    src_ip: str,
    dst_ip: str,
    ttl: int,
    ip_id: int,
    payload_len: int,
) -> ParsedPacket | None:
    if proto_num == _IPPROTO_ICMP and len(l4) >= 8:
        icmp_id, icmp_seq = struct.unpack(">HH", l4[4:8])
        return ParsedPacket(ts, "icmp", src_ip, dst_ip, ttl, ip_id,
                            icmp_id=icmp_id, icmp_seq=icmp_seq, payload_len=payload_len)
    if proto_num == _IPPROTO_ICMPV6 and len(l4) >= 8:
        icmp_id, icmp_seq = struct.unpack(">HH", l4[4:8])
        return ParsedPacket(ts, "icmp6", src_ip, dst_ip, ttl, ip_id,
                            icmp_id=icmp_id, icmp_seq=icmp_seq, payload_len=payload_len)
    if proto_num == _IPPROTO_TCP and len(l4) >= 20:
        src_port, dst_port = struct.unpack(">HH", l4[0:4])
        tcp_seq = struct.unpack(">I", l4[4:8])[0]
        data_offset = (l4[12] >> 4) * 4
        flags = l4[13]
        tcp_payload = max(0, payload_len - data_offset)
        return ParsedPacket(ts, "tcp", src_ip, dst_ip, ttl, ip_id,
                            src_port=src_port, dst_port=dst_port,
                            tcp_seq=tcp_seq, tcp_flags=flags, payload_len=tcp_payload)
    if proto_num == _IPPROTO_UDP and len(l4) >= 8:
        src_port, dst_port, udp_len, _csum = struct.unpack(">HHHH", l4[0:8])
        return ParsedPacket(ts, "udp", src_ip, dst_ip, ttl, ip_id,
                            src_port=src_port, dst_port=dst_port,
                            payload_len=max(0, udp_len - 8))
    return None


def _ipv4_addr(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def _ipv6_addr(raw: bytes) -> str:
    import ipaddress
    return str(ipaddress.IPv6Address(raw))
