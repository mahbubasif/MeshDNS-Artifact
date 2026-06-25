"""
MeshDNS adversarial evaluation primitives (SimPy-free, fast Monte Carlo).

Models signed-quorum rules aligned with firmware:
  q = max(floor(n/2) + 1, MIN_QUORUM), identical-answer quorum, one vote per key.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

MIN_QUORUM = 3
TRUE_IP = "192.168.0.120"
FAKE_IP = "6.6.6.6"
TRUST_ALPHA = 0.30
TRUST_INIT = 0.50


class ByzantineLevel(str, Enum):
    HONEST = "honest"
    L1_FIXED = "l1_fixed"          # consistent wrong IP
    L2_RANDOM = "l2_random"        # random wrong IP each vote
    L3_DELAY = "l3_delay"          # omitted from early collection (timeout pressure)
    L4_EQUIVOCATE = "l4_equivocate"  # two conflicting signed answers (second rejected)
    L5_COLLUDE = "l5_collude"      # same FAKE_IP as other Byzantine voters
    CRASH = "crash"                # no response
    SLOW = "slow"                  # counted as missing before timeout
    GARBAGE = "garbage"            # invalid signature → dropped


def required_quorum(peer_count: int, min_quorum: int = MIN_QUORUM) -> int:
    dynamic_q = (peer_count // 2) + 1
    return max(dynamic_q, min_quorum)


@dataclass
class Voter:
    key_id: str
    physical_id: str
    byzantine: bool = False
    level: ByzantineLevel = ByzantineLevel.HONEST
    has_cache: bool = False
    trust: float = TRUST_INIT
    equivocations: int = 0
    votes_rejected: int = 0

    def cached_ip(self, true_ip: str, collude_ip: str, rng: random.Random) -> Optional[str]:
        if not self.has_cache:
            return None
        if not self.byzantine:
            return true_ip
        if self.level == ByzantineLevel.CRASH:
            return None
        if self.level == ByzantineLevel.L2_RANDOM:
            return f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        if self.level in (ByzantineLevel.L1_FIXED, ByzantineLevel.L5_COLLUDE, ByzantineLevel.L3_DELAY,
                          ByzantineLevel.L4_EQUIVOCATE, ByzantineLevel.SLOW, ByzantineLevel.GARBAGE):
            return collude_ip if self.level == ByzantineLevel.L5_COLLUDE else FAKE_IP
        return true_ip


@dataclass
class RoundOutcome:
    success: bool
    timeout: bool
    false_accept: bool
    accepted_ip: Optional[str]
    winning_votes: int
    required_q: int
    total_votes: int
    equivocation_detected: bool
    latency_ms: float
    vote_histogram: Dict[str, int] = field(default_factory=dict)


@dataclass
class TrustSnapshot:
    round_no: int
    scores: Dict[str, float]


def apply_trust_update(voters: List[Voter], accepted_ip: Optional[str], vote_map: Dict[str, str]) -> None:
    if accepted_ip is None:
        return
    for voter in voters:
        ip = vote_map.get(voter.key_id)
        if ip is None:
            continue
        outcome = 1.0 if ip == accepted_ip else 0.0
        voter.trust = TRUST_ALPHA * outcome + (1.0 - TRUST_ALPHA) * voter.trust


def simulate_vote_round(
    voters: List[Voter],
    *,
    packet_loss: float = 0.0,
    collude_ip: str = FAKE_IP,
    collect_only_slow: bool = False,
    rng: Optional[random.Random] = None,
) -> RoundOutcome:
    """
    One cold-resolution quorum round.

    peer_count is the number of voters that participate (respond or are expected).
    """
    rng = rng or random.Random()
    alive = [v for v in voters if v.level != ByzantineLevel.CRASH]
    peer_count = len(alive)
    q = required_quorum(peer_count)

    votes: Dict[str, str] = {}  # key_id -> ip
    histogram: Dict[str, int] = {}
    equivocation_detected = False
    latency_ms = 1409.0

    for voter in alive:
        if voter.level == ByzantineLevel.GARBAGE:
            voter.votes_rejected += 1
            continue
        if voter.level == ByzantineLevel.SLOW and collect_only_slow:
            continue
        if rng.random() < packet_loss:
            continue

        ip = voter.cached_ip(TRUE_IP, collude_ip, rng)
        if ip is None:
            continue

        if voter.level == ByzantineLevel.L4_EQUIVOCATE:
            alt = "5.6.7.8" if ip != "5.6.7.8" else "1.2.3.4"
            if voter.key_id in votes:
                equivocation_detected = True
                voter.equivocations += 1
                voter.votes_rejected += 1
                voter.trust = TRUST_ALPHA * 0.0 + (1.0 - TRUST_ALPHA) * voter.trust
                continue
            votes[voter.key_id] = ip
            histogram[ip] = histogram.get(ip, 0) + 1
            # second conflicting packet in same round
            if alt != ip:
                equivocation_detected = True
                voter.equivocations += 1
                voter.votes_rejected += 1
                voter.trust = TRUST_ALPHA * 0.0 + (1.0 - TRUST_ALPHA) * voter.trust
            continue

        if voter.key_id in votes:
            voter.votes_rejected += 1
            continue

        votes[voter.key_id] = ip
        histogram[ip] = histogram.get(ip, 0) + 1

    if not histogram:
        return RoundOutcome(
            success=False,
            timeout=True,
            false_accept=False,
            accepted_ip=None,
            winning_votes=0,
            required_q=q,
            total_votes=0,
            equivocation_detected=equivocation_detected,
            latency_ms=latency_ms * 1.0,
            vote_histogram={},
        )

    winning_ip, winning_count = max(histogram.items(), key=lambda kv: kv[1])
    success = winning_count >= q
    false_accept = success and winning_ip != TRUE_IP
    timeout = not success

    if success:
        apply_trust_update(voters, winning_ip, votes)

    return RoundOutcome(
        success=success,
        timeout=timeout,
        false_accept=false_accept,
        accepted_ip=winning_ip if success else None,
        winning_votes=winning_count,
        required_q=q,
        total_votes=sum(histogram.values()),
        equivocation_detected=equivocation_detected,
        latency_ms=latency_ms,
        vote_histogram=histogram,
    )


def build_mesh(
    n_physical: int,
    f_byzantine: int,
    *,
    level: ByzantineLevel = ByzantineLevel.L1_FIXED,
    sybil_per_physical: int = 0,
    byzantine_ids: Optional[List[int]] = None,
) -> List[Voter]:
    """Create voters; sybil_per_physical>0 adds virtual keys on first Byzantine node."""
    byzantine_ids = byzantine_ids or list(range(n_physical - f_byzantine, n_physical))
    voters: List[Voter] = []
    for i in range(n_physical):
        is_byz = i in byzantine_ids
        voters.append(
            Voter(
                key_id=f"node-{i}",
                physical_id=f"phys-{i}",
                byzantine=is_byz,
                level=level if is_byz else ByzantineLevel.HONEST,
            )
        )
    if sybil_per_physical > 0 and f_byzantine > 0:
        byz_phys = f"phys-{byzantine_ids[0]}"
        for k in range(1, sybil_per_physical + 1):
            voters.append(
                Voter(
                    key_id=f"sybil-{byzantine_ids[0]}-{k}",
                    physical_id=byz_phys,
                    byzantine=True,
                    level=level,
                )
            )
    return voters


def configure_cache(
    voters: List[Voter],
    *,
    seed_honest: bool = True,
    seed_byzantine: bool = False,
    distributed_miss: bool = False,
) -> None:
    for voter in voters:
        voter.has_cache = False
    if distributed_miss:
        for voter in voters:
            if voter.byzantine:
                voter.has_cache = True
        return
    for voter in voters:
        if voter.byzantine and seed_byzantine:
            voter.has_cache = True
        if not voter.byzantine and seed_honest:
            voter.has_cache = True


def run_monte_carlo(
    voters: List[Voter],
    rounds: int,
    *,
    packet_loss: float = 0.0,
    seed: int = 42,
) -> Dict[str, float]:
    rng = random.Random(seed)
    successes = timeouts = false_accepts = equivoc = 0
    latencies: List[float] = []

    for _ in range(rounds):
        outcome = simulate_vote_round(voters, packet_loss=packet_loss, rng=rng)
        if outcome.success:
            successes += 1
            latencies.append(outcome.latency_ms)
        if outcome.timeout:
            timeouts += 1
        if outcome.false_accept:
            false_accepts += 1
        if outcome.equivocation_detected:
            equivoc += 1

    return {
        "rounds": rounds,
        "success_rate": successes / rounds,
        "timeout_rate": timeouts / rounds,
        "false_accept_rate": false_accepts / rounds,
        "equivocation_detection_rate": equivoc / rounds,
        "median_latency_ms": statistics.median(latencies) if latencies else float("nan"),
        "peer_count": len([v for v in voters if v.level != ByzantineLevel.CRASH]),
        "required_quorum": required_quorum(len([v for v in voters if v.level != ByzantineLevel.CRASH])),
    }


def sweep_varying_f(
    n: int,
    rounds: int,
    *,
    level: ByzantineLevel = ByzantineLevel.L1_FIXED,
    packet_loss: float = 0.0,
    seed: int = 42,
) -> List[Dict[str, float]]:
    rows = []
    max_f = (n - 1) // 2
    for f in range(0, max_f + 1):
        voters = build_mesh(n, f, level=level)
        configure_cache(voters, seed_honest=True, seed_byzantine=False)
        metrics = run_monte_carlo(voters, rounds, packet_loss=packet_loss, seed=seed + f)
        metrics["n"] = n
        metrics["f"] = f
        metrics["level"] = level.value
        rows.append(metrics)
    return rows


def min_sybil_to_break(n_physical: int, honest_count: int, rounds: int = 200, seed: int = 42) -> Dict[str, int]:
    """Binary search minimum extra Sybil keys on one Byzantine node to force false accept."""
    for k in range(0, n_physical + 3):
        f = 1
        voters = build_mesh(n_physical, f, level=ByzantineLevel.L5_COLLUDE, sybil_per_physical=k)
        configure_cache(voters, distributed_miss=True)
        metrics = run_monte_carlo(voters, rounds, seed=seed)
        if metrics["false_accept_rate"] > 0.5:
            return {"n_physical": n_physical, "min_sybil_keys": k, "required_quorum": metrics["required_quorum"]}
    return {"n_physical": n_physical, "min_sybil_keys": -1, "required_quorum": required_quorum(n_physical)}


def trust_convergence_rounds(
    n: int,
    rounds: int = 100,
    seed: int = 42,
    threshold: float = 0.30,
) -> Dict[str, object]:
    rng = random.Random(seed)
    voters = build_mesh(n, 1, level=ByzantineLevel.L1_FIXED)
    configure_cache(voters, seed_honest=True, seed_byzantine=True)
    trajectory: List[TrustSnapshot] = []
    byz_key = next(v.key_id for v in voters if v.byzantine)

    rounds_to_threshold: Optional[int] = None
    for r in range(1, rounds + 1):
        outcome = simulate_vote_round(voters, rng=rng)
        # trust updated inside simulate_vote_round on success
        trajectory.append(TrustSnapshot(round_no=r, scores={v.key_id: v.trust for v in voters}))
        byz_trust = next(v.trust for v in voters if v.key_id == byz_key)
        if rounds_to_threshold is None and byz_trust < threshold:
            rounds_to_threshold = r

    honest_scores = [v.trust for v in voters if not v.byzantine]
    return {
        "rounds": rounds,
        "byzantine_key": byz_key,
        "rounds_to_trust_below": rounds_to_threshold,
        "final_byzantine_trust": next(v.trust for v in voters if v.key_id == byz_key),
        "final_honest_trust_mean": statistics.mean(honest_scores),
        "trajectory": [{"round": t.round_no, "scores": t.scores} for t in trajectory[::10]],
    }


def ci95(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    return 1.96 * statistics.stdev(values) / math.sqrt(len(values))
