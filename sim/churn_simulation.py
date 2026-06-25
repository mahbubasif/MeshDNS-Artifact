"""
MeshDNS churn simulation — SimPy model of a dynamic IoT mesh.
Includes automated Multi-Core Scalability Sweeps and 
Sim-to-Real Hardware Calibration.
"""

from __future__ import annotations

import random
import statistics
import math
import multiprocessing
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import simpy

# ===================== Simulation parameters =====================
N_NODES = 50
BYZANTINE_FRACTION = 0.15
CHURN_FRACTION = 0.20
CHURN_INTERVAL_S = 60.0
RESTORE_DELAY_S = 15.0
SIM_DURATION_S = 600.0

QUERY_INTERVAL_S = 0.5
TARGET_DOMAIN = "lab-target.local"
TRUE_IP = "192.168.1.10"
MIN_QUORUM = 3
GOSSIP_INTERVAL_S = 60.0
CACHE_TTL_S = 300.0
INITIAL_SEED_FRACTION = 0.20
MIN_INITIAL_SEEDS = 3

GOSSIP_ENABLED = True
VOTING_ENABLED = True
ALLOW_ROOT_FALLBACK = False

# Calibrated latencies (seconds) — Grounded in ESP8266 hardware.
# The default uses the median-ish calibration from successful v6/v7/v9/focused
# BFT observations; CLI --calibration can switch to v6 or v9 directly.
LATENCY_CACHE_HIT_S = 0.00047      # 0.47 ms warm cache
LATENCY_PEER_VOTE_S = 1.40875      # 1.409 s cold signed quorum
VOTE_TIMEOUT_S = 1.900             # Firmware timeout buffer
LATENCY_ROOT_DNS_S = 0.150         # Standard WiFi.hostByName()

# Hardware Reality
UDP_DROP_RATE = 0.218              # Median stress miss rate from v6/v7/v9
RNG_SEED = 42

CALIBRATION_PROFILES = {
    "v6": {
        "cache_hit_s": 0.00047,
        "peer_vote_s": 1.43038,
        "udp_drop_rate": 0.274,
        "source": "benchmark/results/full-evaluation_v6",
    },
    "v9": {
        "cache_hit_s": 0.000456,
        "peer_vote_s": 1.39287,
        "udp_drop_rate": 0.202,
        "source": "benchmark/results/full-evaluation_v9",
    },
    "median": {
        "cache_hit_s": 0.00047,
        "peer_vote_s": 1.40875,
        "udp_drop_rate": 0.218,
        "source": "median of successful v6/v7/v9/focused hardware observations",
    },
}

# ===================== Data structures =====================


@dataclass
class CacheEntry:
    ip: str
    expires_at: float


@dataclass
class NodeStats:
    queries_total: int = 0
    cache_hits: int = 0
    peer_vote_attempts: int = 0
    quorum_hits: int = 0
    root_fallbacks: int = 0
    failed_resolves: int = 0
    query_latencies_ms: List[float] = field(default_factory=list)
    cache_latencies_ms:  List[float] = field(default_factory=list)
    quorum_latencies_ms: List[float] = field(default_factory=list)
    root_latencies_ms:   List[float] = field(default_factory=list)


@dataclass
class ConvergenceSample:
    t: float
    fraction_resolved_via_peers: float

# ===================== Node =====================


class Node:
    def __init__(self, env: simpy.Environment, node_id: int, byzantine: bool, mesh: "Mesh"):
        self.env = env
        self.id = node_id
        self.byzantine = byzantine
        self.mesh = mesh
        self.alive = True
        self.cache: Dict[str, CacheEntry] = {}
        self.peers: List[int] = []
        self.stats = NodeStats()
        env.process(self.query_loop())
        env.process(self.gossip_loop())

    def answer_vote(self, domain: str) -> Optional[str]:
        if not self.alive:
            return None
        entry = self.cache.get(domain)
        if entry and entry.expires_at > self.env.now:
            if self.byzantine:
                return "6.6.6.6"
            return entry.ip
        return None

    def resolve(self, domain: str) -> Tuple[str, str]:
        start = self.env.now
        self.stats.queries_total += 1

        entry = self.cache.get(domain)
        if entry and entry.expires_at > self.env.now:
            yield self.env.timeout(LATENCY_CACHE_HIT_S)
            dt = (self.env.now - start) * 1000
            self.stats.cache_hits += 1
            self.stats.query_latencies_ms.append(dt)
            self.stats.cache_latencies_ms.append(dt)
            return (entry.ip, "cache")

        yield_result = (yield self.env.process(self._peer_vote(domain)) if VOTING_ENABLED else None)
        if yield_result is not None:
            self.cache[domain] = CacheEntry(
                yield_result, self.env.now + CACHE_TTL_S)
            dt = (self.env.now - start) * 1000
            self.stats.quorum_hits += 1
            self.stats.query_latencies_ms.append(dt)
            self.stats.quorum_latencies_ms.append(dt)
            return (yield_result, "peers")

        if not ALLOW_ROOT_FALLBACK:
            dt = (self.env.now - start) * 1000
            self.stats.failed_resolves += 1
            self.stats.query_latencies_ms.append(dt)
            return ("0.0.0.0", "failed")

        yield self.env.timeout(LATENCY_ROOT_DNS_S)
        self.cache[domain] = CacheEntry(TRUE_IP, self.env.now + CACHE_TTL_S)
        dt = (self.env.now - start) * 1000
        self.stats.root_fallbacks += 1
        self.stats.query_latencies_ms.append(dt)
        self.stats.root_latencies_ms.append(dt)
        return (TRUE_IP, "root")

    def _peer_vote(self, domain: str):
        self.stats.peer_vote_attempts += 1
        round_cost = LATENCY_PEER_VOTE_S
        jitter = random.uniform(-0.1, 0.1) * round_cost
        yield self.env.timeout(round_cost + jitter)

        votes: Dict[str, int] = {}
        peers = [self.mesh.nodes[pid]
                 for pid in self.peers if self.mesh.nodes[pid].alive]

        for peer in peers:
            # SIMULATE PHYSICAL UDP DROPS
            # Hardware calibration proves ESP8266s drop packets under load
            if random.random() < UDP_DROP_RATE:
                continue

            ans = peer.answer_vote(domain)
            if ans is not None:
                votes[ans] = votes.get(ans, 0) + 1

        if not votes:
            return None
        winning_ip, count = max(votes.items(), key=lambda kv: kv[1])
        return winning_ip if count >= MIN_QUORUM else None

    def query_loop(self):
        while True:
            jitter = random.uniform(0, QUERY_INTERVAL_S)
            yield self.env.timeout(jitter)
            if self.alive:
                yield self.env.process(self.resolve(TARGET_DOMAIN))
            yield self.env.timeout(QUERY_INTERVAL_S)

    def gossip_loop(self):
        while True:
            yield self.env.timeout(GOSSIP_INTERVAL_S + random.uniform(-5, 5))
            if not self.alive or not GOSSIP_ENABLED:
                continue
            if not self.peers:
                continue
            pid = random.choice(self.peers)
            peer = self.mesh.nodes[pid]
            if not peer.alive or peer.byzantine:
                continue
            entry = peer.cache.get(TARGET_DOMAIN)
            if entry and entry.expires_at > self.env.now:
                self.cache[TARGET_DOMAIN] = CacheEntry(entry.ip, min(
                    entry.expires_at, self.env.now + CACHE_TTL_S))

# ===================== Mesh =====================


class Mesh:
    def __init__(self, env: simpy.Environment, n_nodes: int = N_NODES):
        self.env = env
        self.nodes: List[Node] = []
        self.convergence_log: List[ConvergenceSample] = []
        self.churn_events: List[float] = []

        n_byz = int(n_nodes * BYZANTINE_FRACTION)
        byz_ids = set(random.sample(range(n_nodes), n_byz))
        for i in range(n_nodes):
            self.nodes.append(Node(env, i, i in byz_ids, self))

        # Dynamic Scale: Nodes maintain O(log N) peers to prevent broadcast storms
        max_peers = max(4, int(math.log2(n_nodes) * 1.5))
        for node in self.nodes:
            candidates = [n.id for n in self.nodes if n.id != node.id]
            node.peers = random.sample(
                candidates, min(max_peers, len(candidates)))

        self.seed_initial_caches()
        env.process(self.churn_loop())
        env.process(self.sample_convergence_loop())

    def seed_initial_caches(self):
        honest = [n for n in self.nodes if not n.byzantine]
        if not honest:
            return
        seed_count = max(MIN_INITIAL_SEEDS, int(len(honest) * INITIAL_SEED_FRACTION))
        seed_count = min(seed_count, len(honest))
        for node in random.sample(honest, seed_count):
            node.cache[TARGET_DOMAIN] = CacheEntry(TRUE_IP, self.env.now + CACHE_TTL_S)

    def churn_loop(self):
        yield self.env.timeout(30.0)
        while True:
            live_ids = [n.id for n in self.nodes if n.alive]
            k = int(len(live_ids) * CHURN_FRACTION)
            victims = random.sample(live_ids, k)
            for vid in victims:
                self.nodes[vid].alive = False
                self.nodes[vid].cache.clear()
                self.env.process(self.restore(vid))
            self.churn_events.append(self.env.now)
            yield self.env.timeout(CHURN_INTERVAL_S)

    def restore(self, node_id: int):
        yield self.env.timeout(RESTORE_DELAY_S)
        self.nodes[node_id].alive = True

    def sample_convergence_loop(self):
        while True:
            yield self.env.timeout(2.0)
            honest = [n for n in self.nodes if not n.byzantine]
            if not honest:
                continue
            # Offline/rejoining honest nodes count as not covered. This makes
            # churn recovery visible instead of dropping churned nodes from the
            # denominator and reporting instant convergence.
            got_via_peers = sum(
                1 for n in honest
                if n.alive
                and TARGET_DOMAIN in n.cache
                and n.cache[TARGET_DOMAIN].expires_at > self.env.now
            )
            self.convergence_log.append(ConvergenceSample(
                self.env.now, got_via_peers / len(honest)))

# ===================== Metrics & Sweeps =====================


def apply_calibration(name: str) -> dict:
    global LATENCY_CACHE_HIT_S, LATENCY_PEER_VOTE_S, UDP_DROP_RATE
    profile = CALIBRATION_PROFILES[name]
    LATENCY_CACHE_HIT_S = profile["cache_hit_s"]
    LATENCY_PEER_VOTE_S = profile["peer_vote_s"]
    UDP_DROP_RATE = profile["udp_drop_rate"]
    return profile


def compute_convergence_times(mesh: Mesh) -> List[float]:
    times = []
    for t_event in mesh.churn_events:
        post = [s for s in mesh.convergence_log if s.t >= t_event]
        t_converged = None
        for s in post:
            if s.fraction_resolved_via_peers >= 0.95:
                t_converged = s.t - t_event
                break
        times.append(t_converged if t_converged is not None else float("nan"))
    return times


def mean(values: List[float]) -> float:
    return statistics.mean(values) if values else float("nan")


def median(values: List[float]) -> float:
    return statistics.median(values) if values else float("nan")


def ci95(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0 if values else float("nan")
    return 1.96 * statistics.stdev(values) / math.sqrt(len(values))


def run_single_sim(n_nodes: int, seed: int = RNG_SEED) -> dict:
    """Wrapper for multiprocessing sweeps."""
    random.seed(seed)
    env = simpy.Environment()
    mesh = Mesh(env, n_nodes=n_nodes)
    env.run(until=SIM_DURATION_S)

    ct = compute_convergence_times(mesh)
    ct_valid = [t for t in ct if not math.isnan(t)]
    avg_convergence = mean(ct_valid)

    tot_q = sum(n.stats.queries_total for n in mesh.nodes)
    tot_cache = sum(n.stats.cache_hits for n in mesh.nodes)
    tot_attempts = sum(n.stats.peer_vote_attempts for n in mesh.nodes)
    tot_qh = sum(n.stats.quorum_hits for n in mesh.nodes)
    tot_root = sum(n.stats.root_fallbacks for n in mesh.nodes)
    tot_failed = sum(n.stats.failed_resolves for n in mesh.nodes)
    final_coverage = mesh.convergence_log[-1].fraction_resolved_via_peers * 100 if mesh.convergence_log else 0.0

    return {
        "nodes": n_nodes,
        "seed": seed,
        "convergence_s": avg_convergence,
        "convergence_events": len(ct_valid),
        "total_churn_events": len(ct),
        "quorum_attempt_success_rate": (tot_qh / max(tot_attempts, 1)) * 100,
        "quorum_path_fraction": (tot_qh / max(tot_q, 1)) * 100,
        "cache_hit_fraction": (tot_cache / max(tot_q, 1)) * 100,
        "root_fallback_fraction": (tot_root / max(tot_q, 1)) * 100,
        "failed_fraction": (tot_failed / max(tot_q, 1)) * 100,
        "final_coverage_pct": final_coverage,
        "queries_total": tot_q,
        "peer_vote_attempts": tot_attempts,
    }


def run_single_sim_task(args: Tuple[int, int]) -> dict:
    n_nodes, seed = args
    return run_single_sim(n_nodes, seed=seed)


def summarize_results(results: List[dict]) -> List[dict]:
    summaries = []
    for n_nodes in sorted({r["nodes"] for r in results}):
        group = [r for r in results if r["nodes"] == n_nodes]
        conv = [r["convergence_s"] for r in group if not math.isnan(r["convergence_s"])]
        q_attempt = [r["quorum_attempt_success_rate"] for r in group]
        q_path = [r["quorum_path_fraction"] for r in group]
        coverage = [r["final_coverage_pct"] for r in group]
        summaries.append({
            "nodes": n_nodes,
            "runs": len(group),
            "convergence_mean_s": mean(conv),
            "convergence_median_s": median(conv),
            "convergence_ci95_s": ci95(conv),
            "converged_runs": len(conv),
            "quorum_attempt_success_mean_pct": mean(q_attempt),
            "quorum_attempt_success_median_pct": median(q_attempt),
            "quorum_attempt_success_ci95_pct": ci95(q_attempt),
            "quorum_path_fraction_mean_pct": mean(q_path),
            "final_coverage_mean_pct": mean(coverage),
        })
    return summaries


def fmt_metric(value: float, unit: str = "") -> str:
    if math.isnan(value):
        return "n/a"
    return f"{value:.2f}{unit}"


def run_scalability_sweep(seed_count: int = 5):
    print("\n" + "=" * 60)
    print("STARTING MULTI-CORE SCALABILITY SWEEP")
    print("=" * 60)
    node_counts = [5, 50, 100, 250, 500, 1000]

    cores = multiprocessing.cpu_count()
    print(f"[*] Distributing {len(node_counts) * seed_count} simulations across {cores} logical cores...")
    print(f"[*] Calibration: cache={LATENCY_CACHE_HIT_S*1000:.3f} ms, "
          f"BFT={LATENCY_PEER_VOTE_S*1000:.1f} ms, UDP drop={UDP_DROP_RATE*100:.1f}%")
    print(f"[*] Root fallback: {'enabled' if ALLOW_ROOT_FALLBACK else 'disabled'}; "
          f"target={TARGET_DOMAIN} -> {TRUE_IP}\n")

    tasks = [
        (n_nodes, RNG_SEED + seed_offset)
        for n_nodes in node_counts
        for seed_offset in range(seed_count)
    ]
    with multiprocessing.Pool(processes=cores) as pool:
        results = pool.map(run_single_sim_task, tasks)

    summaries = summarize_results(results)

    print(
        f"{'Nodes':<7} | {'Conv mean/med ±95CI (s)':<25} | "
        f"{'Quorum attempt success':<24} | {'Quorum path':<12} | {'Final coverage'}"
    )
    print("-" * 105)
    for r in summaries:
        conv = (
            "n/a"
            if math.isnan(r["convergence_mean_s"])
            else f"{r['convergence_mean_s']:.2f}/{r['convergence_median_s']:.2f} ±{r['convergence_ci95_s']:.2f}"
        )
        q_attempt = (
            f"{r['quorum_attempt_success_mean_pct']:.1f}/"
            f"{r['quorum_attempt_success_median_pct']:.1f} ±"
            f"{r['quorum_attempt_success_ci95_pct']:.1f}%"
        )
        print(
            f"{r['nodes']:<7} | {conv:<25} | "
            f"{q_attempt:<24} | "
            f"{r['quorum_path_fraction_mean_pct']:.2f}%{'':<7} | "
            f"{r['final_coverage_mean_pct']:.1f}%"
        )

    print("\n[+] Sweep complete. Simulation uses ESP8266-calibrated latency/drop parameters.")
    print("[*] 'Quorum path' is quorum hits divided by all queries; 'quorum attempt success' is quorum hits divided by peer-vote attempts.")

# ===================== Main =====================


def main():
    import argparse
    global N_NODES, BYZANTINE_FRACTION, CHURN_FRACTION, CHURN_INTERVAL_S, SIM_DURATION_S
    global GOSSIP_ENABLED, VOTING_ENABLED, ALLOW_ROOT_FALLBACK, TARGET_DOMAIN, TRUE_IP

    ap = argparse.ArgumentParser(description="MeshDNS churn simulation")
    ap.add_argument("-n", "--nodes", type=int, default=N_NODES)
    ap.add_argument("-b", "--byzantine", type=float,
                    default=BYZANTINE_FRACTION)
    ap.add_argument("--calibration", choices=sorted(CALIBRATION_PROFILES),
                    default="median",
                    help="Hardware calibration profile for warm-cache latency, cold-BFT latency, and UDP drop rate")
    ap.add_argument("--seeds", type=int, default=5,
                    help="Number of random seeds per node count in --sweep")
    ap.add_argument("--root-fallback", action="store_true",
                    help="Enable root DNS fallback after failed peer quorum (disabled by default to model .local BFT benchmarks)")
    ap.add_argument("--target-domain", default=TARGET_DOMAIN)
    ap.add_argument("--target-ip", default=TRUE_IP)
    ap.add_argument("--sweep", action="store_true",
                    help="Run multi-core scalability sweep (5-1000 nodes)")
    args = ap.parse_args()

    profile = apply_calibration(args.calibration)
    BYZANTINE_FRACTION = args.byzantine
    ALLOW_ROOT_FALLBACK = args.root_fallback
    TARGET_DOMAIN = args.target_domain
    TRUE_IP = args.target_ip

    if args.sweep:
        print(f"[*] Using calibration '{args.calibration}' ({profile['source']})")
        run_scalability_sweep(seed_count=max(1, args.seeds))
    else:
        N_NODES = args.nodes
        print(
            f"Running single hardware-grounded simulation for N={N_NODES} "
            f"with calibration '{args.calibration}'...")
        res = run_single_sim(N_NODES, seed=RNG_SEED)
        print(
            "Result: "
            f"Convergence = {fmt_metric(res['convergence_s'], 's')}, "
            f"Quorum attempt success = {res['quorum_attempt_success_rate']:.1f}%, "
            f"Quorum path fraction = {res['quorum_path_fraction']:.2f}%, "
            f"Cache hit fraction = {res['cache_hit_fraction']:.1f}%, "
            f"Final coverage = {res['final_coverage_pct']:.1f}%"
        )


if __name__ == "__main__":
    main()
