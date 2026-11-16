"""In-container probe for the isolated-sidecar-network spikes.

Run as ``python3 -c <this source> '<json ops>'`` inside a spike container (the
host scripts do this). Prints one JSON line: a list of results aligned with the
ops. Standard library only; needs ``ip`` (iproute2) for the ``addrs`` op.

Ops run in order; consecutive network ops run concurrently, ``sh`` and
``sleep`` run alone (so a route change applies to the probes after it).

Network outcomes: ``connected``, ``timeout``, ``refused``, ``enetunreach``,
``ehostunreach``, ``resolve-failed``, ``http`` (a response was read), or
``error: ...``. A probe that could not run reports ``error`` and must never be
read as "blocked".
"""

# A probe reports every failure as data, never as a crash.
# ruff: noqa: BLE001

import errno
import http.client
import json
import os
import random
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

SEQUENTIAL = {"sh", "sleep"}


def outcome(exc):
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, socket.gaierror):
        return "resolve-failed"
    if isinstance(exc, OSError) and exc.errno in errno.errorcode:
        return errno.errorcode[exc.errno].lower()
    return f"error: {type(exc).__name__}: {exc}"


def connect(host, port, timeout):
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    family, _, _, _, address = infos[0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(address)
    except BaseException:
        sock.close()
        raise
    return sock, address[0]


def op_tcp(op):
    try:
        sock, address = connect(op["host"], op["port"], op.get("timeout", 3))
    except Exception as exc:
        return {"outcome": outcome(exc)}
    sock.close()
    return {"outcome": "connected", "address": address}


def nameserver():
    try:
        with open("/etc/resolv.conf") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    return parts[1]
    except OSError:
        pass
    return None


def op_udp_dns(op):
    server = op["server"]
    if server == "@resolv":
        server = nameserver()
        if server is None:
            return {"outcome": "error: no nameserver in /etc/resolv.conf"}
    qid = random.randint(0, 0xFFFF)
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in op.get("name", "example.com").split("."):
        query += bytes([len(label)]) + label.encode()
    query += b"\x00" + struct.pack(">HH", 1, 1)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    over_tcp = op.get("tcp", False)
    sock = socket.socket(family, socket.SOCK_STREAM if over_tcp else socket.SOCK_DGRAM)
    sock.settimeout(op.get("timeout", 3))
    try:
        if over_tcp:  # RFC 1035 4.2.2: two-byte length prefix
            sock.connect((server, op.get("port", 53)))
            sock.sendall(struct.pack(">H", len(query)) + query)
            data = b""
            while len(data) < 2 or len(data) < 2 + struct.unpack(">H", data[:2])[0]:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            data = data[2:]
        else:
            sock.sendto(query, (server, op.get("port", 53)))
            data, _ = sock.recvfrom(4096)
    except Exception as exc:
        return {"outcome": outcome(exc), "server": server}
    finally:
        sock.close()
    if len(data) < 12:
        # Something accepted the query but sent no DNS reply.
        return {"outcome": "no-answer", "server": server, "bytes": len(data)}
    _, flags, _, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
    return {
        "outcome": "answered",
        "server": server,
        "rcode": flags & 0xF,
        "answers": ancount,
    }


def op_resolve(op):
    try:
        infos = socket.getaddrinfo(op["name"], None)
    except Exception as exc:
        return {"outcome": outcome(exc)}
    return {"outcome": "resolved", "addresses": sorted({i[4][0] for i in infos})}


def _target(op):
    url = urllib.parse.urlsplit(op["url"])
    if op.get("proxy"):
        proxy = urllib.parse.urlsplit(op["proxy"])
        return proxy.hostname, proxy.port or 80, op["url"]
    path = (url.path or "/") + ("?" + url.query if url.query else "")
    return url.hostname, url.port or 80, path


def op_http(op):
    host, port, target = _target(op)
    start = time.monotonic()
    try:
        conn = http.client.HTTPConnection(host, port, timeout=op.get("timeout", 10))
        body = op.get("body")
        conn.request(
            op.get("method", "GET"),
            target,
            body=body.encode() if body else None,
            headers={"Content-Type": "application/json"} if body else {},
        )
        resp = conn.getresponse()
        data = resp.read(4096)
        conn.close()
    except Exception as exc:
        return {"outcome": outcome(exc)}
    return {
        "outcome": "http",
        "status": resp.status,
        "body": data[:600].decode("utf-8", "replace"),
        "server": resp.getheader("Server"),
        "elapsed_s": round(time.monotonic() - start, 3),
    }


def op_stream(op):
    """Time each line (NDJSON) or each ``data:`` line (SSE) as it arrives."""
    host, port, target = _target(op)
    sse = op.get("sse", False)
    start = time.monotonic()
    arrivals, excerpt, buf = [], [], b""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=op.get("timeout", 30))
        body = op.get("body")
        conn.request(
            op.get("method", "GET"),
            target,
            body=body.encode() if body else None,
            headers={"Content-Type": "application/json"} if body else {},
        )
        resp = conn.getresponse()
        headers_s = time.monotonic() - start
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                break
            now = round(time.monotonic() - start, 3)
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line or (sse and not line.startswith(b"data:")):
                    continue
                arrivals.append(now)
                if len(excerpt) < 2:
                    excerpt.append(line[:100].decode("utf-8", "replace"))
        conn.close()
    except Exception as exc:
        return {"outcome": outcome(exc), "arrivals": arrivals}
    return {
        "outcome": "http",
        "status": resp.status,
        "headers_s": round(headers_s, 3),
        "arrivals": arrivals,
        "excerpt": excerpt,
        "total_s": round(time.monotonic() - start, 3),
    }


def op_raw(op):
    """Send ``payload`` and return the first bytes of the reply."""
    try:
        sock, _ = connect(op["host"], op["port"], op.get("timeout", 5))
    except Exception as exc:
        return {"outcome": outcome(exc)}
    reply = b""
    try:
        sock.sendall(op["payload"].encode())
        while len(reply) < 2048:
            data = sock.recv(2048)
            if not data:
                break
            reply += data
    except Exception as exc:
        kind = outcome(exc)
        if not reply:
            sock.close()
            return {"outcome": "connected", "reply": "", "after": kind}
    sock.close()
    return {"outcome": "connected", "reply": reply[:600].decode("utf-8", "replace")}


def op_read(op):
    try:
        with open(op["path"]) as fh:
            return {"outcome": "read", "text": fh.read()[:4000]}
    except OSError as exc:
        return {"outcome": outcome(exc)}


def op_addrs(op):
    result = {"outcome": "read", "ipv4": [], "ipv6": []}
    for family, key in (("-4", "ipv4"), ("-6", "ipv6")):
        out = subprocess.run(
            ["ip", "-o", family, "addr", "show", "scope", "global"],
            capture_output=True,
            check=False,
            text=True,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                result[key].append({"ifname": parts[1], "cidr": parts[3]})
    for family, key in (("-4", "routes4"), ("-6", "routes6")):
        result[key] = subprocess.run(
            ["ip", family, "route", "show"], capture_output=True, check=False, text=True
        ).stdout
    result["hostname"] = socket.gethostname()
    return result


def op_sh(op):
    try:
        proc = subprocess.run(
            op["cmd"],
            capture_output=True,
            check=False,
            text=True,
            timeout=op.get("timeout", 30),
        )
    except Exception as exc:
        return {"outcome": outcome(exc)}
    return {
        "outcome": "ran",
        "rc": proc.returncode,
        "out": (proc.stdout + proc.stderr)[-2000:],
    }


def op_sleep(op):
    time.sleep(op["seconds"])
    return {"outcome": "slept"}


OPS = {
    "tcp": op_tcp,
    "udp_dns": op_udp_dns,
    "resolve": op_resolve,
    "http": op_http,
    "stream": op_stream,
    "raw": op_raw,
    "read": op_read,
    "addrs": op_addrs,
    "sh": op_sh,
    "sleep": op_sleep,
}


def run_one(op):
    try:
        result = OPS[op["kind"]](op)
    except Exception as exc:  # a bug in the probe, never a network verdict
        result = {"outcome": f"error: probe failed: {exc!r}"}
    result["op"] = op
    return result


def main():
    ops = json.loads(sys.argv[1])
    results = [None] * len(ops)
    index = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        while index < len(ops):
            if ops[index]["kind"] in SEQUENTIAL:
                results[index] = run_one(ops[index])
                index += 1
                continue
            end = index
            while end < len(ops) and ops[end]["kind"] not in SEQUENTIAL:
                end += 1
            for offset, result in enumerate(pool.map(run_one, ops[index:end])):
                results[index + offset] = result
            index = end
    sys.stdout.write(json.dumps(results) + "\n")
    sys.stdout.flush()
    os._exit(0)


main()
