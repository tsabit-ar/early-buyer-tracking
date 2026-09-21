"""Scoring engine for Early Buyer Scanner (EBRS).

Implements Section 8 of the PRD:
1. Early Entry Score (40%): Time elapsed after launch (tiers 0-2m, 2-5m, etc.).
2. Buy Size Score (25%): Percentile ranking among token buyers, with min-max fallback for <5 buyers.
3. Accumulation Score (15%): Multi-buy accumulation behavior.
4. Holding/Exit Score (20%): Token retention and exit ratio.
"""

from typing import Dict, List, Optional, Tuple, Union

from config import DEFAULT_SCORING_WEIGHTS, settings
from models.schemas import ConfidenceEnum, ScoreBreakdown, WalletProfile


def calculate_early_entry_score(
    time_after_launch: Optional[Union[int, float]],
    launch_confidence: Union[str, ConfidenceEnum] = ConfidenceEnum.HIGH,
) -> float:
    """Calculate Early Entry Score (0-100) based on time elapsed after launch.
    
    Tiers (PRD Section 8.3):
    - 0-2 min (0-120s): 100
    - >2-5 min (121-300s): 90
    - >5-10 min (301-600s): 75
    - >10-20 min (601-1200s): 55
    - >20-60 min (1201-3600s): 30
    - >60 min (>3600s): 10
    
    Fallback: If launch_time is unknown/LOW, return baseline 50.
    """
    conf_str = launch_confidence.value if isinstance(launch_confidence, ConfidenceEnum) else str(launch_confidence)
    if time_after_launch is None or conf_str.upper() == "LOW":
        return 50.0

    seconds = max(0, int(time_after_launch))
    if seconds <= 120:
        return 100.0
    elif seconds <= 300:
        return 90.0
    elif seconds <= 600:
        return 75.0
    elif seconds <= 1200:
        return 55.0
    elif seconds <= 3600:
        return 30.0
    else:
        return 10.0


def calculate_buy_size_scores(profiles: List[WalletProfile]) -> Dict[str, float]:
    """Calculate Buy Size Scores (0-100) for all buyer profiles of a token.
    
    Percentile-based for >= 5 buyers (PRD Section 8.4):
    - P99: 100
    - P95: 90
    - P90: 80
    - P75: 60
    - P50: 40
    - <P50: 20
    
    Fallback for < 5 buyers: Min-max scaling in range [20, 100].
    """
    if not profiles:
        return {}

    n = len(profiles)
    scores: Dict[str, float] = {}
    amounts = [p.total_buy_amount for p in profiles]

    # Fallback for < 5 buyers: Min-Max scaling
    if n < 5:
        min_amt = min(amounts)
        max_amt = max(amounts)
        for p in profiles:
            if max_amt > min_amt:
                scaled = ((p.total_buy_amount - min_amt) / (max_amt - min_amt)) * 80.0 + 20.0
            else:
                scaled = 60.0
            scores[p.wallet_address] = round(scaled, 2)
        return scores

    # Percentile ranking for >= 5 buyers
    for p in profiles:
        amt = p.total_buy_amount
        # Percentile rank: fraction of buyers with total_buy_amount <= amt
        count_less_equal = sum(1 for a in amounts if a <= amt)
        percentile = (count_less_equal / n) * 100.0

        if percentile >= 99.0:
            score = 100.0
        elif percentile >= 95.0:
            score = 90.0
        elif percentile >= 90.0:
            score = 80.0
        elif percentile >= 75.0:
            score = 60.0
        elif percentile >= 50.0:
            score = 40.0
        else:
            score = 20.0

        scores[p.wallet_address] = score

    return scores


def calculate_accumulation_score(buy_count: int) -> float:
    """Calculate Accumulation Score (0-100) based on verified buy count (PRD Section 8.5).
    
    - 1 buy: 50 (baseline)
    - 2 buys: 75
    - >= 3 buys: 100
    """
    if buy_count >= 3:
        return 100.0
    elif buy_count == 2:
        return 75.0
    else:
        return 50.0


def calculate_holding_score(exit_ratio: float) -> float:
    """Calculate Holding/Exit Score (0-100) based on retention ratio (PRD Section 8.6).
    
    retention = max(0.0, 1.0 - exit_ratio)
    holding_score = retention * 100
    """
    retention = max(0.0, min(1.0, 1.0 - max(0.0, exit_ratio)))
    return round(retention * 100.0, 2)


def calculate_wallet_score(
    profile: WalletProfile,
    buy_size_score: float,
    launch_confidence: Union[str, ConfidenceEnum] = ConfidenceEnum.HIGH,
) -> Tuple[float, ScoreBreakdown]:
    """Compute the weighted composite score (0-100) and complete audit breakdown."""
    weights = settings.scoring_weights

    early_entry_score = calculate_early_entry_score(
        profile.time_after_launch,
        launch_confidence=launch_confidence,
    )
    accumulation_score = calculate_accumulation_score(profile.buy_count)
    holding_score = calculate_holding_score(profile.exit_ratio)

    w_early = weights.get("early_entry", 0.40)
    w_size = weights.get("buy_size", 0.25)
    w_acc = weights.get("accumulation", 0.15)
    w_hold = weights.get("holding_exit", 0.20)

    composite = (
        (early_entry_score * w_early)
        + (buy_size_score * w_size)
        + (accumulation_score * w_acc)
        + (holding_score * w_hold)
    )

    final_score = float(round(max(0.0, min(100.0, composite))))

    breakdown = ScoreBreakdown(
        wallet_address=profile.wallet_address,
        early_entry_score=early_entry_score,
        buy_size_score=buy_size_score,
        accumulation_score=accumulation_score,
        holding_score=holding_score,
        final_score=final_score,
        breakdown_details={
            "weights": weights,
            "time_after_launch_sec": profile.time_after_launch,
            "total_buy_amount": profile.total_buy_amount,
            "buy_count": profile.buy_count,
            "exit_ratio": profile.exit_ratio,
        },
    )

    return final_score, breakdown


def score_and_rank_buyers(
    profiles: List[WalletProfile],
    launch_confidence: Union[str, ConfidenceEnum] = ConfidenceEnum.HIGH,
) -> List[Tuple[WalletProfile, ScoreBreakdown]]:
    """Score and rank a list of early buyer profiles in descending order of score.
    
    Args:
        profiles: List of WalletProfile models.
        launch_confidence: Confidence of token launch timestamp.
        
    Returns:
        List of tuples (ranked_profile, score_breakdown) sorted by score descending.
    """
    if not profiles:
        return []

    # 1. Compute collective buy size scores
    buy_size_scores = calculate_buy_size_scores(profiles)

    # 2. Compute individual composite scores
    results: List[Tuple[WalletProfile, ScoreBreakdown]] = []
    for p in profiles:
        size_score = buy_size_scores.get(p.wallet_address, 50.0)
        final_score, breakdown = calculate_wallet_score(
            p,
            buy_size_score=size_score,
            launch_confidence=launch_confidence,
        )
        p.score = final_score
        results.append((p, breakdown))

    # 3. Sort by final score descending; tiebreaker: earlier first_buy_time
    results.sort(
        key=lambda item: (
            item[0].score,
            -(item[0].first_buy_time or 9999999999),
        ),
        reverse=True,
    )

    return results
