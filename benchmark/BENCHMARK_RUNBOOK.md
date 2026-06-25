# Benchmark Runbook

This is the canonical reproduction guide for MeshDNS hardware benchmarks. It assumes two computers and physical ESP8266-class nodes on the same private LAN. Use `benchmark/ADVERSARIAL_EVAL.md` only after this base setup works.

For optional 4-node BFT notes, see `benchmark/test_scripts/TESTBED_4NODE.md`.

## 1. What You Need

- One benchmark host that runs the Python scripts.
- One target host that advertises a reachable `.local` mDNS/Bonjour name.
- At least four ESP8266 nodes for a minimal f=1 BFT quorum test; five nodes match the archived paper testbed.
- One shared LAN/subnet for the benchmark host, target host, and all ESP8266 nodes.

Network ports used:

- UDP `5353` for MeshDNS peer traffic.
- UDP `8080` for ESP8266 telemetry to the benchmark host.
- UDP `8081` for benchmark commands from the host to ESP8266 nodes.

## 2. Install Host Dependencies

From the repository root on the benchmark host:

```bash
cd /path/to/MeshDNS-Artifact
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install pyserial zeroconf requests pynacl
```

## 3. Configure Firmware

Before flashing, configure each node with the same Wi-Fi, mesh secret, and command token:

```cpp
#define WIFI_SSID "YourTestbedWifi"
#define WIFI_PASSWORD "YourWifiPassword"
#define NETWORK_PSK "same-secret-on-every-node"
#define TESTBED_CONTROL_TOKEN "same-command-token-on-host-and-nodes"
#define BYZANTINE_MODE 0
```

Important:

- `NETWORK_PSK` must match on every admitted node.
- `TESTBED_CONTROL_TOKEN` must match on every node and the benchmark host.
- Reflash all nodes after changing shared secrets or keyed-domain hashing.
- Clear old caches after flashing with `clear all` in the command center.

For Byzantine tests, reflash only the adversarial node(s) with `BYZANTINE_MODE 1`; keep the resolver honest.

## 4. Identify Target and Node Addresses

Find the target host's `.local` name and LAN IP.

On macOS:

```bash
scutil --get LocalHostName
ipconfig getifaddr en0
```

On Linux:

```bash
hostname
hostname -I
```

If the target host name is `lab-target` and its LAN IP is `192.168.1.10`, use:

```text
lab-target.local
192.168.1.10
```

Verify from the benchmark host:

```bash
python3 - <<'PYCODE'
import socket
print(socket.gethostbyname("lab-target.local"))
PYCODE
```

Use the command center to discover ESP8266 nodes. Replace the broadcast address with your subnet's broadcast address:

```bash
python3 benchmark/test_scripts/testbed_command_center.py --broadcast 192.168.1.255
```

At the prompt:

```text
nodes
discover
peers
stats
```

Record all node IPs and choose one resolver that will remain honest during BFT and adversarial tests.

Recommended environment variables:

```bash
export MESHDNS_BROADCAST=192.168.1.255
export MESHDNS_TARGET_DOMAIN=lab-target.local
export MESHDNS_TARGET_IP=192.168.1.10
export MESHDNS_NODES=192.168.1.21,192.168.1.22,192.168.1.23,192.168.1.24,192.168.1.25
export MESHDNS_RESOLVER=192.168.1.22
```

Common private broadcast examples include `192.168.0.255`, `192.168.1.255`, and `10.42.0.255`; use the one that matches your LAN.

## 5. Interactive Command Center

Script: `benchmark/test_scripts/testbed_command_center.py`

Start it with:

```bash
python3 benchmark/test_scripts/testbed_command_center.py --broadcast "$MESHDNS_BROADCAST"
```

Token resolution order:

1. `--token` argument.
2. `firmware/meshdns_node/config.h` (`TESTBED_CONTROL_TOKEN`).
3. `MESHDNS_CONTROL_TOKEN` environment variable.
4. Built-in fallback (`CHANGE_ME_TESTBED_CONTROL_TOKEN`).

If nodes were flashed with a custom token:

```bash
python3 benchmark/test_scripts/testbed_command_center.py \
  --broadcast "$MESHDNS_BROADCAST" \
  --token 'YOUR_SHARED_TOKEN'
```

Manual smoke test:

```text
clear all
seed lab-target.local 192.168.1.10 all
resolve lab-target.local all
```

Manual cold BFT test with three seeded voters and one resolver:

```text
clear all
seed lab-target.local 192.168.1.10 192.168.1.21
seed lab-target.local 192.168.1.10 192.168.1.23
seed lab-target.local 192.168.1.10 192.168.1.24
resolve lab-target.local 192.168.1.22
```

Success should include:

```text
source=peer_quorum
```

You can automate the same shape with:

```text
bftcold lab-target.local 192.168.1.10 192.168.1.22
```

For public domains, CDN answers can vary by network and time. For reproducible cache experiments, prefer a lab-controlled target name/IP or record the exact returned IP in telemetry.

## 6. Canonical Hardware Benchmark

Script: `benchmark/test_scripts/run_all_benchmarks.py`

This is the main benchmark entry point. It runs:

- OS mDNS baseline.
- MeshDNS warm-cache resolution.
- MeshDNS cold-cache BFT quorum resolution.
- Optional manual Byzantine test.
- MeshDNS stress test.

Basic run:

```bash
python3 benchmark/test_scripts/run_all_benchmarks.py \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --expected-nodes 5 \
  --nodes "$MESHDNS_NODES" \
  --resolver "$MESHDNS_RESOLVER" \
  --skip-byzantine-prompt
```

Result folder:

```text
benchmark/results/hardware_YYYYMMDD_HHMMSS/
```

Key outputs:

- `meshdns_evaluation.json` contains the full raw benchmark result.
- `telemetry_log.csv` contains raw ESP8266 telemetry.
- `mdns_meshdns_summary.csv` is a compact table for plotting and paper tables.

Useful flags:

```bash
python3 benchmark/test_scripts/run_all_benchmarks.py \
  --resolver "$MESHDNS_RESOLVER" \
  --bft-attempts 3 \
  --bft-resolve-timeout 15 \
  --bft-settle-sec 2 \
  --bft-stable-sec 12 \
  --stress-requests 500 \
  --stress-delay-ms 20 \
  --results-root benchmark/results
```

## 7. Focused BFT Runner

Script: `benchmark/test_scripts/bft_benchmark.py`

Use this when you only want to test cold-cache BFT quorum behavior.

Honest BFT example:

```bash
python3 benchmark/test_scripts/bft_benchmark.py \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --nodes "$MESHDNS_NODES" \
  --resolver "$MESHDNS_RESOLVER" \
  --attempts 3
```

One-Byzantine example:

```bash
python3 benchmark/test_scripts/bft_benchmark.py \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --nodes "$MESHDNS_NODES" \
  --resolver "$MESHDNS_RESOLVER" \
  --byzantine-ip 192.168.1.21 \
  --resolve-timeout-s 15 \
  --attempts 5
```

Replace `192.168.1.21` with a non-resolver node that has been flashed as Byzantine.

Outputs:

- `benchmark/results/bft_YYYYMMDD_HHMMSS/bft_evaluation.json`
- `benchmark/results/bft_YYYYMMDD_HHMMSS/bft_telemetry.csv`
- `benchmark/results/bft_YYYYMMDD_HHMMSS/bft_summary.csv`

## 8. mDNS Comparison Report

Script: `benchmark/test_scripts/mdns_comparison.py`

This script reads a `meshdns_evaluation.json` produced by the canonical hardware benchmark and regenerates a compact mDNS-vs-MeshDNS report:

```bash
python3 benchmark/test_scripts/mdns_comparison.py \
  --input benchmark/results/hardware_YYYYMMDD_HHMMSS/meshdns_evaluation.json
```

Outputs are written next to the selected input:

- `mdns_meshdns_comparison.csv`
- `mdns_meshdns_comparison.json`

## 9. Optional Video Coexistence Run

This is valid evidence only if you actually run it and archive the generated result folder.

1. Connect a video-streaming client, benchmark host, and ESP8266 nodes to the same Wi-Fi.
2. Start a 4K stream and let it stabilize for 1-2 minutes.
3. Run the canonical benchmark with:

```bash
python3 benchmark/test_scripts/run_all_benchmarks.py \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --expected-nodes 5 \
  --nodes "$MESHDNS_NODES" \
  --resolver "$MESHDNS_RESOLVER" \
  --stress-requests 500 \
  --stress-delay-ms 20 \
  --skip-byzantine-prompt \
  --video-coexistence "4K video stream on another client connected to the same AP"
```

Do not claim video coexistence unless the corresponding result folder is included.

## 10. Optional and Legacy Tools

`benchmark/test_scripts/serial_query.py` is optional single-node serial debugging. The canonical benchmarks use UDP command/telemetry instead.

`gateway/server.py`, `gateway/relay_node.py`, and `benchmark/comparison_script.py` are legacy/prototype components. They are not required for the current ESP8266 peer-quorum benchmark path.

## 11. Troubleshooting

| Symptom | Check |
|---------|-------|
| `COMMAND_REJECTED: bad_token` | Command token does not match `TESTBED_CONTROL_TOKEN`. |
| No nodes discovered | Confirm same subnet, correct broadcast address, and UDP `8080` reachability. |
| No BFT quorum | Run `discover`, `peers`, and `nodes`; confirm enough peers and seeded caches. |
| mDNS baseline fails | Confirm the target host's `.local` name resolves from the benchmark host. |
| Stress test success is very low | Clear and reseed; confirm `CACHE_SEEDED` telemetry before stress traffic starts. |
