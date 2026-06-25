# 4-Node ESP8266 MeshDNS Testbed

Four ESP8266 nodes are enough to exercise the f=1 BFT quorum path: seed three honest voters, query from the fourth node, and confirm `source=peer_quorum`. For the full paper-style workflow and 5-node adversarial matrix, use `benchmark/BENCHMARK_RUNBOOK.md` and `benchmark/ADVERSARIAL_EVAL.md`.

## Network Layout

- Benchmark host: runs the command center and writes CSV logs.
- Target host: advertises a `.local` mDNS/Bonjour name, such as `lab-target.local`.
- Four ESP8266 boards: MeshDNS fleet on the same 2.4 GHz Wi-Fi/LAN.

Use an isolated router, lab AP, or hotspot. All devices must share the same subnet.

## Firmware Configuration

Edit `firmware/meshdns_node/config.h` before flashing:

```cpp
#define WIFI_SSID "YourTestbedWifi"
#define WIFI_PASSWORD "YourWifiPassword"
#define NETWORK_PSK "same-secret-on-every-node"
#define TESTBED_CONTROL_TOKEN "same-command-token-on-host-and-nodes"
#define BYZANTINE_MODE 0
```

Flash the same honest firmware to all four ESP8266 boards. Each board keeps its own generated keypair in EEPROM, so the binaries can be identical for honest-node tests.

## Start The Command Center

From the repository root on the benchmark host:

```bash
export MESHDNS_CONTROL_TOKEN='same-command-token-on-host-and-nodes'
python3 benchmark/hardware/testbed_command_center.py --broadcast 192.168.1.255
```

Change `192.168.1.255` to your subnet broadcast address. Common examples are `192.168.0.255`, `192.168.1.255`, and `10.42.0.255`.

The script listens on UDP `8080`, sends commands on UDP `8081`, and appends telemetry rows to `meshdns_4node_benchmark.csv` unless a different output path is provided.

## Useful Commands

Inside the `MeshDNS>` prompt:

```text
nodes
discover
peers
stats
clear
seed lab-target.local 192.168.1.10 all
resolve lab-target.local all
resolve lab-target.local 192.168.1.24
bftcold lab-target.local 192.168.1.10 192.168.1.24
```

Use `nodes` to see which ESP8266 boards are alive. Use a node IP instead of `all` when you want to trigger one resolver and observe how the other peers answer voting requests.

## Recommended Flow

1. Boot all four ESP8266 nodes and wait until `nodes` shows all of them.
2. Run `discover`, then `peers`, until each node reports roughly three peers.
3. Run `clear all`.
4. Seed the target mapping into three honest nodes:

```text
seed lab-target.local 192.168.1.10 192.168.1.21
seed lab-target.local 192.168.1.10 192.168.1.22
seed lab-target.local 192.168.1.10 192.168.1.23
```

5. Query the fourth node:

```text
resolve lab-target.local 192.168.1.24
```

The resulting telemetry should include `source=peer_quorum` and a detail like `votes:3;quorum:3;bft_ms:<n>`.

For a Byzantine case, flash one voter with `BYZANTINE_MODE 1`, keep the resolver honest, and repeat the cold-cache query. The integrity metric is whether the resolver avoids accepting the fake IP.

## Target Host

On macOS:

```bash
hostname
scutil --get LocalHostName
```

On Linux:

```bash
hostname
hostname -I
```

If the local hostname is `lab-target`, test with `lab-target.local`.

## Firewall Notes

Allow UDP `8080` inbound on the benchmark host so telemetry can reach the Python listener. ESP8266 nodes need UDP `5353` for MeshDNS peer traffic and UDP `8081` for command-center commands.

## Troubleshooting

`COMMAND_REJECTED: bad_token` means the UDP command token did not match the firmware's `TESTBED_CONTROL_TOKEN`. Pass the exact token explicitly or let the command center read it from `firmware/meshdns_node/config.h`.

For the cold-cache BFT test, success means:

```text
RESOLVE_OK ... source=peer_quorum,detail=votes:3;quorum:3;bft_ms:<n>
```

If you see fewer than three votes, rerun `discover`, confirm all nodes show `peers=3`, and confirm the three seeded nodes show `cache_used=1` before querying the resolver.
