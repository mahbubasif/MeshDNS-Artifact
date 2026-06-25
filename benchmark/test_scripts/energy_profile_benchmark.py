#!/usr/bin/env python3
"""
Energy profiler runner for firmware/energy_profiler_node.

This does not use or modify the production MeshDNS firmware. Flash the temporary
energy_profiler_node sketch to one ESP8266, wire the INA219 to an Arduino Uno,
then use this runner to trigger an in-device warm-cache lookup loop.

Manual mode:
  python3 benchmark/test_scripts/energy_profile_benchmark.py --node 192.168.1.24

Automatic INA219 capture:
  python3 benchmark/test_scripts/energy_profile_benchmark.py \
    --node 192.168.1.24 --ina-port /dev/cu.usbmodemXXXX
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import socket
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_TELEMETRY_PORT = 8080
DEFAULT_COMMAND_PORT = 8081
DEFAULT_TARGET_DOMAIN = "lab-target.local"
DEFAULT_TARGET_IP = "192.168.1.10"
DEFAULT_LOOPS = 1000
DEFAULT_RESULTS_ROOT = Path(__file__).resolve().parents[1] / "results"
DEFAULT_CONTROL_TOKEN = "CHANGE_ME_TESTBED_CONTROL_TOKEN"
# Firmware arming delay (energy_profiler_node delay before loop).
ARMING_DELAY_SEC = 3.0
# Conservative upper bound from hardware (~8.5 us/lookup); used only for timeouts.
US_PER_LOOKUP_EST = 10.0
MIN_DONE_TIMEOUT_SEC = 30.0
CONFIG_TOKEN_PATTERN = re.compile(r'^\s*#define\s+TESTBED_CONTROL_TOKEN\s+"([^"]*)"', re.MULTILINE)

RE_VOLTAGE = re.compile(r"Voltage\(V\)\s*:\s*([0-9.]+)")
RE_CURRENT = re.compile(r"Current\(mA\)\s*:\s*([0-9.]+)")
RE_POWER = re.compile(r"Power\(mW\)\s*:\s*([0-9.]+)")
RE_ENERGY = re.compile(r"Total[_ ]?Energy\(mJ\)\s*:\s*([0-9.]+)")


@dataclass
class InaSample:
    host_time: float
    voltage_v: Optional[float]
    current_ma: Optional[float]
    power_mw: Optional[float]
    total_energy_mj: float
    raw: str


class InaSerialReader:
    def __init__(self, port: str, baud: int) -> None:
        self.port = port
        self.baud = baud
        self.samples: List[InaSample] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._serial = None

    def start(self) -> None:
        try:
            import serial  # type: ignore
        except ImportError as exc:
            raise RuntimeError("pyserial is required for --ina-port. Install pyserial or omit --ina-port.") from exc

        self._serial = serial.Serial(self.port, self.baud, timeout=0.1)
        self._serial.reset_input_buffer()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._serial:
            self._serial.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            raw = self._serial.readline().decode("ascii", errors="ignore").strip()
            if not raw:
                continue
            sample = self._parse_line(raw)
            if sample:
                self.samples.append(sample)

    @staticmethod
    def _float_match(regex: re.Pattern[str], raw: str) -> Optional[float]:
        match = regex.search(raw)
        return float(match.group(1)) if match else None

    def _parse_line(self, raw: str) -> Optional[InaSample]:
        energy = self._float_match(RE_ENERGY, raw)
        if energy is None:
            return None
        return InaSample(
            host_time=time.time(),
            voltage_v=self._float_match(RE_VOLTAGE, raw),
            current_ma=self._float_match(RE_CURRENT, raw),
            power_mw=self._float_match(RE_POWER, raw),
            total_energy_mj=energy,
            raw=raw,
        )

    def samples_between(self, start: float, end: float) -> List[InaSample]:
        return [sample for sample in self.samples if start <= sample.host_time <= end]

    def sample_before(self, t: float) -> Optional[InaSample]:
        candidates = [sample for sample in self.samples if sample.host_time <= t]
        return candidates[-1] if candidates else None

    def sample_after(self, t: float) -> Optional[InaSample]:
        for sample in self.samples:
            if sample.host_time >= t:
                return sample
        return None


class TelemetryListener:
    def __init__(self, port: int, node_ip: str) -> None:
        self.port = port
        self.node_ip = node_ip
        self.events: List[Dict[str, Any]] = []
        self._stop = threading.Event()
        self._cv = threading.Condition()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", port))
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            payload = data.decode("utf-8", errors="replace").strip()
            parsed = self._parse_payload(payload)
            if not parsed:
                continue
            esp_millis, node_id, event, message = parsed
            entry = {
                "host_time": time.time(),
                "ip": addr[0],
                "esp_millis": esp_millis,
                "node_id": node_id,
                "event": event,
                "message": message,
                "fields": parse_fields(message),
            }
            with self._cv:
                self.events.append(entry)
                self._cv.notify_all()
            if addr[0] == self.node_ip:
                print(f"[telemetry] {event}: {message}")

    @staticmethod
    def _parse_payload(payload: str) -> Optional[tuple[str, str, str, str]]:
        parts = payload.split("|", 3)
        if len(parts) != 4:
            return None
        return parts[0], parts[1], parts[2], parts[3]

    def wait_for(self, event_name: str, timeout: float) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        start_index = len(self.events)
        with self._cv:
            while True:
                for event in self.events[start_index:]:
                    if event["ip"] == self.node_ip and event["event"] == event_name:
                        return event
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=remaining)

    def find_event_after(self, after_event: Dict[str, Any], event_name: str) -> Optional[Dict[str, Any]]:
        after_time = after_event["host_time"]
        for event in self.events:
            if (
                event["ip"] == self.node_ip
                and event["event"] == event_name
                and event["host_time"] >= after_time
            ):
                return event
        return None

    def wait_for_done_after(self, after_event: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
        after_time = after_event["host_time"]
        deadline = time.time() + timeout
        with self._cv:
            while True:
                for event in self.events:
                    if (
                        event["ip"] == self.node_ip
                        and event["event"] == "ENERGY_LOOP_DONE"
                        and event["host_time"] >= after_time
                    ):
                        return event
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(timeout=min(remaining, 0.25))


def parse_fields(message: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for part in message.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    if args.config.exists():
        text = args.config.read_text()
        match = CONFIG_TOKEN_PATTERN.search(text)
        if match:
            return match.group(1)
    return DEFAULT_CONTROL_TOKEN


def estimate_loop_duration_sec(loops: int) -> float:
    return loops * US_PER_LOOKUP_EST / 1_000_000.0


def compute_done_timeout_sec(loops: int, user_timeout: float) -> float:
    """Scale wait for DONE: arming delay + estimated loop + margin (WDT-safe firmware yields)."""
    estimated = ARMING_DELAY_SEC + estimate_loop_duration_sec(loops) * 2.0 + 20.0
    return max(user_timeout, MIN_DONE_TIMEOUT_SEC, estimated)


def synthetic_start_event(
    armed_event: Dict[str, Any],
    domain: str,
    loops: int,
) -> Dict[str, Any]:
    starts_in_ms = float(armed_event["fields"].get("starts_in_ms", str(ARMING_DELAY_SEC * 1000)))
    return {
        "host_time": armed_event["host_time"] + starts_in_ms / 1000.0,
        "ip": armed_event["ip"],
        "esp_millis": armed_event.get("esp_millis", ""),
        "node_id": armed_event.get("node_id", ""),
        "event": "ENERGY_LOOP_START",
        "message": f"domain={domain},loops={loops}",
        "fields": {"domain": domain, "loops": str(loops)},
        "synthetic": True,
    }


def wait_for_benchmark_events(
    listener: TelemetryListener,
    domain: str,
    loops: int,
    done_timeout: float,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    armed = listener.wait_for("ENERGY_LOOP_ARMED", timeout=10.0)
    if not armed:
        raise RuntimeError("Timed out waiting for ENERGY_LOOP_ARMED telemetry")

    print(f"[*] Waiting up to {done_timeout:.0f}s for ENERGY_LOOP_DONE ({loops} loops)...")
    done = listener.wait_for_done_after(armed, done_timeout)
    if not done:
        raise RuntimeError(
            f"Timed out waiting for ENERGY_LOOP_DONE ({loops} loops, {done_timeout:.0f}s). "
            "Check ESP power/Wi-Fi; for very large --loops reflash energy_profiler_node with WDT yields."
        )

    start = listener.find_event_after(armed, "ENERGY_LOOP_START")
    if start:
        print("[+] ENERGY_LOOP_START received")
    else:
        print(
            "[!] ENERGY_LOOP_START not received (UDP often lost before CPU-bound loop); "
            "using synthetic start (+3s after ARMED)"
        )
        start = synthetic_start_event(armed, domain, loops)

    return armed, start, done


def send_energy_command(args: argparse.Namespace, token: str) -> None:
    payload = f"{token}|ENERGY_WARM_LOOP|{args.domain}={args.target_ip},{args.loops}"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload.encode("utf-8"), (args.node, args.command_port))
    finally:
        sock.close()
    print(f"[+] Sent ENERGY_WARM_LOOP to {args.node}:{args.command_port}")
    print(f"    {args.domain}={args.target_ip},{args.loops}")


def summarize_energy(
    ina: InaSerialReader,
    armed_event: Dict[str, Any],
    start_event: Dict[str, Any],
    done_event: Dict[str, Any],
    loops: int,
) -> Dict[str, Any]:
    # Give the INA logger a moment to emit the sample following LOOP_DONE.
    time.sleep(0.5)

    baseline_samples = [
        sample for sample in ina.samples_between(armed_event["host_time"], start_event["host_time"])
        if sample.power_mw is not None
    ]
    start_sample = ina.sample_before(start_event["host_time"])
    end_sample = ina.sample_after(done_event["host_time"]) or ina.sample_before(done_event["host_time"])

    result: Dict[str, Any] = {
        "ina_samples": len(ina.samples),
        "start_sample": start_sample.__dict__ if start_sample else None,
        "end_sample": end_sample.__dict__ if end_sample else None,
    }

    if not start_sample or not end_sample:
        result["error"] = "missing INA start/end sample"
        return result

    gross_mj = end_sample.total_energy_mj - start_sample.total_energy_mj
    baseline_power_mw = statistics.mean(sample.power_mw for sample in baseline_samples) if baseline_samples else None
    esp_elapsed_ms = float(done_event["fields"].get("elapsed_ms", "nan"))
    idle_estimate_mj = (baseline_power_mw * (esp_elapsed_ms / 1000.0)) if baseline_power_mw is not None else None

    result.update({
        "gross_loop_energy_mj": gross_mj,
        "gross_energy_per_lookup_mj": gross_mj / loops,
        "baseline_power_mw": baseline_power_mw,
        "idle_energy_during_esp_loop_mj": idle_estimate_mj,
        "idle_corrected_loop_energy_mj": (gross_mj - idle_estimate_mj) if idle_estimate_mj is not None else None,
        "idle_corrected_per_lookup_mj": ((gross_mj - idle_estimate_mj) / loops) if idle_estimate_mj is not None else None,
    })
    return result


def write_outputs(args: argparse.Namespace, data: Dict[str, Any], ina: Optional[InaSerialReader]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.results_root / f"energy_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=False)

    json_path = out_dir / "energy_profile.json"
    json_path.write_text(json.dumps(data, indent=2))

    if ina:
        csv_path = out_dir / "ina219_samples.csv"
        with csv_path.open("w", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=["host_time", "voltage_v", "current_ma", "power_mw", "total_energy_mj", "raw"],
            )
            writer.writeheader()
            for sample in ina.samples:
                writer.writerow(sample.__dict__)

    print(f"[+] Results saved to {out_dir}")
    return out_dir


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Run ESP8266 warm-cache energy profiling loop")
    parser.add_argument("--node", required=True, help="IP address of ESP8266 running firmware/energy_profiler_node")
    parser.add_argument("--domain", default=DEFAULT_TARGET_DOMAIN)
    parser.add_argument("--target-ip", default=DEFAULT_TARGET_IP)
    parser.add_argument("--loops", type=int, default=DEFAULT_LOOPS)
    parser.add_argument("--command-port", type=int, default=DEFAULT_COMMAND_PORT)
    parser.add_argument("--telemetry-port", type=int, default=DEFAULT_TELEMETRY_PORT)
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Minimum seconds to wait for ENERGY_LOOP_DONE (auto-scaled up with --loops)",
    )
    parser.add_argument("--ina-port", default="", help="Optional Arduino/INA219 serial port for automatic energy capture")
    parser.add_argument("--ina-baud", type=int, default=115200)
    parser.add_argument("--token", default="")
    parser.add_argument("--config", type=Path, default=repo_root / "firmware" / "meshdns_node" / "config.h")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.loops = max(1, args.loops)
    token = resolve_token(args)

    listener = TelemetryListener(args.telemetry_port, args.node)
    listener.start()

    ina: Optional[InaSerialReader] = None
    if args.ina_port:
        ina = InaSerialReader(args.ina_port, args.ina_baud)
        ina.start()
        print("[*] Reading INA219 serial for 2 seconds before triggering...")
        time.sleep(2.0)
    else:
        print("[*] No --ina-port provided. Manual mode:")
        print("    Note Total_Energy(mJ) when ENERGY_LOOP_START prints, then again at ENERGY_LOOP_DONE.")

    try:
        done_timeout = compute_done_timeout_sec(args.loops, args.timeout)
        send_energy_command(args, token)
        armed, start, done = wait_for_benchmark_events(
            listener, args.domain, args.loops, done_timeout
        )

        fields = done["fields"]
        print("\n[+] ESP loop complete")
        print(f"    hits={fields.get('hits', '?')}/{fields.get('loops', args.loops)}")
        print(f"    elapsed_ms={fields.get('elapsed_ms', '?')}")
        print(f"    avg_lookup_us={fields.get('avg_us', '?')}")

        data: Dict[str, Any] = {
            "metadata": {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "node": args.node,
                "domain": args.domain,
                "target_ip": args.target_ip,
                "loops": args.loops,
                "ina_port": args.ina_port or None,
            },
            "telemetry": {
                "armed": armed,
                "start": start,
                "done": done,
                "start_synthetic": bool(start.get("synthetic")),
            },
        }

        if ina and armed:
            energy = summarize_energy(ina, armed, start, done, args.loops)
            data["energy"] = energy
            if "gross_energy_per_lookup_mj" in energy:
                print("\n[+] INA219 energy estimate")
                print(f"    gross_loop_energy_mJ={energy['gross_loop_energy_mj']:.3f}")
                print(f"    gross_per_lookup_mJ={energy['gross_energy_per_lookup_mj']:.6f}")
                if energy.get("idle_corrected_per_lookup_mj") is not None:
                    print(f"    baseline_power_mW={energy['baseline_power_mw']:.3f}")
                    print(f"    idle_corrected_per_lookup_mJ={energy['idle_corrected_per_lookup_mj']:.6f}")
            else:
                print(f"[!] INA219 capture incomplete: {energy.get('error')}")
        else:
            print("\nManual formula:")
            print(f"    energy_per_lookup_mJ = (end_Total_Energy_mJ - start_Total_Energy_mJ) / {args.loops}")

        write_outputs(args, data, ina)
    finally:
        if ina:
            ina.stop()
        listener.stop()


if __name__ == "__main__":
    main()
