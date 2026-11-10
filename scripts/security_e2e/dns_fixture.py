"""Tiny authoritative UDP fixture, run only inside the disposable Squid container.

All DNS answers are fixture addresses. No request is forwarded to an external
resolver. The test changes /tmp/security-dns-phase to trigger rebinding.
"""

import ipaddress
import socket
import struct
from pathlib import Path


def answer(packet, phase):
    offset = 12
    labels = []
    while packet[offset]:
        length = packet[offset]
        offset += 1
        labels.append(packet[offset : offset + length].decode("ascii"))
        offset += length
    offset += 1
    kind, cls = struct.unpack("!HH", packet[offset : offset + 4])
    question = packet[12 : offset + 4]
    name = ".".join(labels)
    addresses = []
    if kind == 1:
        if name == "mixed.example.test":
            addresses = ["192.0.2.123", "127.0.0.1"]
        elif name == "rebind.example.test":
            addresses = ["192.0.2.123"] if phase == "public" else ["127.0.0.1"]
        elif name == "private.example.test":
            addresses = ["169.254.169.254"]
    if kind == 28:
        if name == "mapped.example.test":
            addresses = ["::ffff:127.0.0.1"]
        elif name == "nat64.example.test":
            addresses = ["64:ff9b::a9fe:a9fe"]
    records = b""
    for address in addresses:
        data = ipaddress.ip_address(address).packed
        records += b"\xc0\x0c" + struct.pack("!HHIH", kind, cls, 0, len(data)) + data
    return (
        packet[:2]
        + struct.pack("!HHHHH", 0x8180, 1, len(addresses), 0, 0)
        + question
        + records
    )


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 53))
        Path("/tmp/security-dns-ready").touch()
        while True:
            packet, peer = server.recvfrom(4096)
            server.sendto(
                answer(packet, Path("/tmp/security-dns-phase").read_text()), peer
            )


if __name__ == "__main__":
    main()
