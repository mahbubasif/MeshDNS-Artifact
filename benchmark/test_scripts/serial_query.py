"""
Serial bridge: send a domain query to an ESP8266 MeshDNS node and parse
the response, returning resolution path + latency.

Each node is connected via USB serial; the firmware treats stdin lines
as queries and emits log lines of the form:

    [QUERY] Resolving: example.com
    [CACHE HIT] 0.12 ms
    [PEER QUORUM] 15.4 ms
    [ROOT DNS]   180.2 ms
    [SUCCESS] example.com -> 93.184.216.34
    [FAILED] Unable to resolve example.com

Use:
    from serial_query import serial_resolve
    lat_ms, path, ip = serial_resolve("/dev/ttyUSB0", "example.com")
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import serial


RE_CACHE  = re.compile(r"\[CACHE HIT\]\s+([\d.]+)\s+ms")
RE_QUORUM = re.compile(r"\[PEER QUORUM\]\s+([\d.]+)\s+ms")
RE_ROOT   = re.compile(r"\[ROOT DNS[^\]]*\].*?([\d.]+)\s+ms")
RE_OK     = re.compile(r"\[SUCCESS\]\s+\S+\s+->\s+([\d.]+)")
RE_FAIL   = re.compile(r"\[FAILED\]")


@dataclass
class ResolutionResult:
    latency_ms: float
    path: str              # "cache" | "peers" | "root" | "fail"
    ip: Optional[str]


def serial_resolve(port: str, domain: str, baud: int = 115200,
                   timeout_s: float = 3.0) -> ResolutionResult:
    with serial.Serial(port, baud, timeout=0.1) as ser:
        ser.reset_input_buffer()
        ser.write(f"{domain}\n".encode())
        ser.flush()

        t0 = time.perf_counter()
        deadline = t0 + timeout_s
        latency = None
        path = "fail"
        ip = None

        while time.perf_counter() < deadline:
            line = ser.readline().decode("ascii", errors="ignore").strip()
            if not line:
                continue
            if m := RE_CACHE.search(line):
                latency = float(m.group(1)); path = "cache"
            elif m := RE_QUORUM.search(line):
                latency = float(m.group(1)); path = "peers"
            elif m := RE_ROOT.search(line):
                latency = float(m.group(1)); path = "root"
            elif m := RE_OK.search(line):
                ip = m.group(1)
                break
            elif RE_FAIL.search(line):
                path = "fail"
                break

        if latency is None:
            latency = (time.perf_counter() - t0) * 1000
        return ResolutionResult(latency, path, ip)


def list_ports():
    from serial.tools import list_ports
    return [(p.device, p.description) for p in list_ports.comports()]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="COM port (Windows) or /dev/tty* (Linux)")
    ap.add_argument("--domain", default="example.com")
    ap.add_argument("-n", type=int, default=10, help="iterations")
    args = ap.parse_args()

    paths = {"cache": 0, "peers": 0, "root": 0, "fail": 0}
    lat = []
    for i in range(args.n):
        r = serial_resolve(args.port, args.domain)
        paths[r.path] += 1
        if r.path != "fail":
            lat.append(r.latency_ms)
        print(f"  {i+1:3d}: {r.path:6s} {r.latency_ms:7.2f} ms -> {r.ip}")
        time.sleep(0.5)

    print(f"\nPaths: {paths}")
    if lat:
        import statistics
        print(f"Latency (ms): mean {statistics.mean(lat):.2f}  "
              f"median {statistics.median(lat):.2f}  "
              f"min {min(lat):.2f}  max {max(lat):.2f}")
