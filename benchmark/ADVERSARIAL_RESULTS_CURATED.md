# Adversarial Result Index

This file identifies the archived adversarial runs that are most useful for independently checking the MeshDNS security claims. It is intentionally a short index; the raw JSON and CSV files remain in `benchmark/results/`.

Archived result files preserve the private LAN addresses and `.local` hostnames observed during collection. Those values are part of the empirical record and are not required for reproducing the experiments on another subnet.

## Selection Criteria

Runs are listed here when they satisfy at least one of these criteria:

- They support the primary integrity claim: no accepted wrong IP under the stated adversarial condition.
- They demonstrate an expected limitation, such as insufficient honest voters at f=2 on a 5-node quorum-3 mesh.
- They provide a clear negative result, such as the Sybil weakness under shared-key admission without per-device PKI.

## Recommended Archived Runs

| Scenario | Folder | Key result |
|----------|--------|------------|
| Simulation matrix | `benchmark/results/adversarial_sim_20260525_164210/` | Vary-f and collusion simulations show `false_accept_rate=0` within the modeled BFT bound; Sybil simulations show failures once one physical node contributes enough identities. |
| Hardware f=0 baseline | `benchmark/results/adversarial_f0_20260526_003110/` | `success_rate=60%`, `false_accept_rate=0%`; timeouts are availability/harness events, not wrong-IP acceptance. |
| Hardware f=1 fixed wrong IP | `benchmark/results/adversarial_f1_20260526_003649/` | `success_rate=80%`, `false_accept_rate=0%`; strongest archived one-Byzantine hardware run. |
| Hardware f=2 fixed wrong IP | `benchmark/results/adversarial_f2_20260526_004338/` | `success_rate=0%`, `false_accept_rate=0%`; expected rejection because only two honest voters remain for quorum 3. |
| Hardware equivocation | `benchmark/results/hw_l4_20260525_174557/` | `success_rate=60%`, `false_accept_rate=0%`; integrity holds, while equivocation telemetry should be interpreted conservatively. |
| Hardware Sybil stress | `benchmark/results/hw_sybil3_20260525_181451/` | `success_rate=80%`, `false_accept_rate=40%`; documents the known no-PKI Sybil limitation. |

## Supported Claims

1. Hardware f=1 fixed-wrong-answer runs did not accept the adversarial fake IP in the recommended archived run.
2. Hardware f=2 on five nodes is an integrity-preserving rejection case, not an availability success case.
3. Sybil identities from one admitted physical node can break the shared-key model, motivating per-device admission or PKI in future work.
4. Simulation runs are useful for scale trends; hardware runs are the source for ESP8266 timing and RF/harness behavior.

## Raw Files

Each hardware result folder generally contains:

- `adversarial_hardware.json`
- `adversarial_summary.csv`
- `adversarial_telemetry.csv`

The simulation result folder contains `adversarial_simulation.json`.
