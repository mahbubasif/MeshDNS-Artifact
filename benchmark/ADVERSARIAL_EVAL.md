# MeshDNS Adversarial Evaluation Guide

This guide extends `benchmark/BENCHMARK_RUNBOOK.md` with the adversarial hardware and simulation workflows. Complete the base runbook first: install Python dependencies, discover ESP8266 node IPs, verify the target `.local` hostname, and confirm one honest cold-cache BFT run.

The commands below use example addresses from a private lab subnet. Replace every address, hostname, and node role with values from your own testbed.

## Testbed Roles

| Role | Requirement |
|------|-------------|
| Target host | Any computer on the same LAN that advertises a `.local` mDNS/Bonjour name. It does not run benchmark scripts. |
| Benchmark host | Any computer on the same LAN that runs Python, receives UDP telemetry on `8080`, and sends commands on UDP `8081`. |
| Mesh nodes | Five ESP8266-class nodes on the same Wi-Fi/LAN. Four nodes are enough for a minimal f=1 quorum test; five nodes are used for the archived paper runs. |

```text
target host (.local name + LAN IP)
        ^
        | same private LAN, e.g. 192.168.1.0/24
        v
benchmark host --commands/telemetry--> ESP8266 MeshDNS nodes
```

## Configure Your Lab Values

From the repository root on the benchmark host:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install pyserial zeroconf requests pynacl
```

Discover the target host's `.local` name and LAN IP. On macOS, for example:

```bash
scutil --get LocalHostName
ipconfig getifaddr en0
```

If the local host name is `lab-target` and the IP is `192.168.1.10`, the MeshDNS benchmark target is `lab-target.local -> 192.168.1.10`.

Discover ESP8266 nodes with the command center, using your subnet broadcast address:

```bash
python3 benchmark/hardware/testbed_command_center.py --broadcast 192.168.1.255
```

At the `MeshDNS>` prompt:

```text
nodes
```

Record the node IPs and choose one resolver that will remain honest during cold-cache tests. Example environment:

```bash
export MESHDNS_BROADCAST=192.168.1.255
export MESHDNS_TARGET_DOMAIN=lab-target.local
export MESHDNS_TARGET_IP=192.168.1.10
export MESHDNS_NODES=192.168.1.21,192.168.1.22,192.168.1.23,192.168.1.24,192.168.1.25
export MESHDNS_RESOLVER=192.168.1.22
export MESHDNS_BYZANTINE_1=192.168.1.21
export MESHDNS_BYZANTINE_2=192.168.1.23
export MESHDNS_BYZANTINE_3=192.168.1.24
```

Keep the resolver out of the Byzantine set.

## Firmware Modes

For honest nodes:

```cpp
#define BYZANTINE_MODE 0
#define BYZANTINE_LEVEL 1
#define BYZANTINE_SYBIL_EXTRA_KEYS 0
```

For one fixed-wrong-answer Byzantine node:

```cpp
#define BYZANTINE_MODE 1
#define BYZANTINE_LEVEL 1
#define BYZANTINE_SYBIL_EXTRA_KEYS 0
```

Other adversarial modes used by this artifact:

| Mode | Purpose |
|------|---------|
| `BYZANTINE_LEVEL 1` | Always vote for the configured fake IP. |
| `BYZANTINE_LEVEL 4` | Emit conflicting signed votes to test equivocation handling. |
| `BYZANTINE_SYBIL_EXTRA_KEYS 3` | Add extra identities from one physical node to demonstrate the no-PKI Sybil limitation. |

There is no runtime switch for Byzantine behavior in the firmware used for these experiments. Reflash only the boards whose Byzantine flags change, wait for stable heartbeats, and then run the corresponding benchmark.

## Smoke Test

Run one all-honest cold-cache BFT attempt before any adversarial run:

```bash
python3 benchmark/hardware/bft_benchmark.py \
  --nodes "$MESHDNS_NODES" \
  --resolver "$MESHDNS_RESOLVER" \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --attempts 1
```

Success should include `RESOLVE_OK`, `source=peer_quorum`, and the target IP. Fix Wi-Fi, token, subnet, or node discovery issues before running adversarial experiments.

## Simulation Baseline

Simulation requires no ESP8266 hardware:

```bash
python3 sim/adversarial_evaluation.py --suite all --quick
```

Full simulation for paper-scale curves:

```bash
python3 sim/adversarial_evaluation.py --suite vary-f --n 5 7 10 20 --rounds 100
python3 sim/adversarial_evaluation.py --suite all --rounds 100
```

Results are written under `benchmark/results/adversarial_sim_<timestamp>/`.

## Hardware f=0 Baseline

Flash all nodes as honest, then run:

```bash
python3 benchmark/hardware/adversarial_benchmark.py \
  --nodes "$MESHDNS_NODES" \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --resolver "$MESHDNS_RESOLVER" \
  --byzantine-ips "" \
  --rounds 5 \
  --label hw_f0
```

Expected integrity result: `false_accept_rate` should be `0`. Hardware availability depends on RF conditions and harness timing, so report `success_rate` and `timeout_rate` separately.

## Hardware f=1 Fixed Wrong Answer

Reflash one non-resolver node with `BYZANTINE_MODE 1`, `BYZANTINE_LEVEL 1`, and no Sybil keys:

```bash
python3 benchmark/hardware/adversarial_benchmark.py \
  --nodes "$MESHDNS_NODES" \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --resolver "$MESHDNS_RESOLVER" \
  --byzantine-ips "$MESHDNS_BYZANTINE_1" \
  --rounds 5 \
  --label hw_f1_l1
```

Paper-safe claim to check: the resolver should not accept the configured wrong IP. Availability may degrade under attack, but `false_accept_rate` is the primary integrity metric.

## Equivocation Scenario

Reflash one non-resolver Byzantine node:

```cpp
#define BYZANTINE_MODE 1
#define BYZANTINE_LEVEL 4
#define BYZANTINE_SYBIL_EXTRA_KEYS 0
```

Then run:

```bash
python3 benchmark/hardware/adversarial_benchmark.py \
  --nodes "$MESHDNS_NODES" \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --resolver "$MESHDNS_RESOLVER" \
  --byzantine-ips "$MESHDNS_BYZANTINE_1" \
  --rounds 5 \
  --label hw_l4
```

Success criteria: `false_accept_rate == 0`; telemetry may include `EQUIVOC_DETECTED` rows if the resolver observes conflicting signed votes from the same key.

## Sybil Scenario

Reflash one non-resolver Byzantine node:

```cpp
#define BYZANTINE_MODE 1
#define BYZANTINE_LEVEL 1
#define BYZANTINE_SYBIL_EXTRA_KEYS 3
```

Then run:

```bash
python3 benchmark/hardware/adversarial_benchmark.py \
  --nodes "$MESHDNS_NODES" \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --resolver "$MESHDNS_RESOLVER" \
  --byzantine-ips "$MESHDNS_BYZANTINE_1" \
  --rounds 5 \
  --label hw_sybil3
```

If `false_accept_rate > 0`, report it as a known limitation of shared-key admission without per-device PKI.

## f Sweep

The sweep helper prints a flash checklist for each f value. The ordered Byzantine pool must contain non-resolver node IPs from your own testbed:

```bash
python3 benchmark/hardware/adversarial_sweep.py \
  --nodes "$MESHDNS_NODES" \
  --byzantine-pool "$MESHDNS_BYZANTINE_1,$MESHDNS_BYZANTINE_2,$MESHDNS_BYZANTINE_3" \
  --resolver "$MESHDNS_RESOLVER" \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --max-f 2 \
  --rounds 5 \
  --interactive
```

For a 5-node testbed with quorum 3, f=2 leaves only two honest voters when seeding honest voters only. A `RESOLVE_FAIL` with `false_accept_rate == 0` is therefore the expected integrity-preserving result, not a benchmark failure.

## Scenario File

`benchmark/adversarial_scenarios.json` is a template. Replace every `REPLACE_WITH_*` value before using `--scenarios`; the hardware runner rejects placeholder Byzantine IPs to avoid accidentally publishing lab-specific or invalid runs.

Example:

```bash
python3 benchmark/hardware/adversarial_benchmark.py \
  --nodes "$MESHDNS_NODES" \
  --scenarios benchmark/adversarial_scenarios.json \
  --scenario-id hw_f1_l4_equivoc \
  --broadcast "$MESHDNS_BROADCAST" \
  --target-domain "$MESHDNS_TARGET_DOMAIN" \
  --target-ip "$MESHDNS_TARGET_IP" \
  --resolver "$MESHDNS_RESOLVER"
```

## Outputs

Hardware runs write:

- `benchmark/results/<label>_<timestamp>/adversarial_hardware.json`
- `benchmark/results/<label>_<timestamp>/adversarial_summary.csv`
- `benchmark/results/<label>_<timestamp>/adversarial_telemetry.csv`

New sweep runs also write `benchmark/results/adversarial_sweep_summary.json`; the archived f-sweep evidence in this repository is stored in the per-f result folders listed in `benchmark/ADVERSARIAL_RESULTS_CURATED.md`.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `Found 0 nodes` | Wrong broadcast address, nodes not on the same LAN, or firewall blocking UDP `8080`/`8081`. |
| `COMMAND_REJECTED bad_token` | Host token does not match `TESTBED_CONTROL_TOKEN` in firmware. |
| Quorum always fails | Resolver may be in the Byzantine set, too few voters were seeded, or peers are not stable. |
| `TIMEOUT` every round | Wrong resolver IP or host timeout too short for the firmware vote window. |
| Success with the wrong IP | Treat as a security finding; inspect `false_accept_rate`, `byzantine_ips`, and seed policy. |

For raw evidence selection and paper-facing result notes, see `benchmark/ADVERSARIAL_RESULTS_CURATED.md`.
