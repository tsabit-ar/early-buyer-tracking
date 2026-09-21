"""Wallet analyzer module for Early Buyer Scanner (EBRS).

Aggregates verified BUY transactions into comprehensive early buyer profiles
(FR-08, FR-09, FR-12) and formats time-after-launch metrics.
"""

from typing import Any, Dict, List, Optional, Union

from models.schemas import (
    ClassificationEnum,
    ConfidenceEnum,
    TransactionClassification,
    WalletProfile,
)


def format_time_delta(seconds: Optional[Union[int, float]]) -> str:
    """Format a time delta in seconds into a readable string (e.g. +2m14s, +45s, +1h12m).
    
    Args:
        seconds: Elapsed time in seconds or None if unknown.
        
    Returns:
        Formatted string or 'N/A'.
    """
    if seconds is None:
        return "N/A"

    sec = int(seconds)
    prefix = "+" if sec >= 0 else "-"
    abs_sec = abs(sec)

    if abs_sec < 60:
        return f"{prefix}{abs_sec}s"
    elif abs_sec < 3600:
        minutes = abs_sec // 60
        rem_sec = abs_sec % 60
        return f"{prefix}{minutes}m{rem_sec:02d}s"
    else:
        hours = abs_sec // 3600
        rem_min = (abs_sec % 3600) // 60
        return f"{prefix}{hours}h{rem_min:02d}m"


def build_buyer_profile(
    wallet_address: str,
    token_address: str,
    launch_time: Optional[int],
    tx_classifications: List[TransactionClassification],
    holder_data: Optional[Dict[str, Any]] = None,
) -> Optional[WalletProfile]:
    """Construct a WalletProfile by aggregating verified BUY transactions.
    
    Args:
        wallet_address: Target wallet address.
        token_address: Token mint address.
        launch_time: Unix timestamp of token launch.
        tx_classifications: List of all classified transactions for this wallet.
        holder_data: Optional current holder statistics from Solscan.
        
    Returns:
        WalletProfile if the wallet has at least one verified BUY, otherwise None.
    """
    clean_wallet = wallet_address.strip()
    clean_token = token_address.strip()

    # Filter verified BUY transactions for this specific wallet
    buys = [
        tx for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.classification == ClassificationEnum.BUY
    ]

    if not buys:
        return None

    # Chronological sort
    buys.sort(key=lambda tx: tx.block_time)

    first_buy = buys[0]
    first_buy_time = first_buy.block_time
    first_buy_slot = first_buy.slot
    first_buy_sig = first_buy.signature
    first_buy_amount = max(0.0, first_buy.token_change)

    total_buy_amount = sum(max(0.0, b.token_change) for b in buys)
    buy_count = len(buys)

    time_after_launch: Optional[int] = None
    if launch_time is not None and first_buy_time is not None:
        time_after_launch = first_buy_time - launch_time

    # Current holder state integration
    current_holding = 0.0
    holder_rank: Optional[int] = None
    holder_percentage: Optional[float] = None

    if holder_data:
        current_holding = float(holder_data.get("amount", 0.0))
        holder_rank = holder_data.get("rank")
        holder_percentage = holder_data.get("percentage")

    # Aggregate confidence: HIGH if any buy has HIGH confidence, else MEDIUM
    any_high = any(b.confidence == ConfidenceEnum.HIGH for b in buys)
    confidence = ConfidenceEnum.HIGH if any_high else ConfidenceEnum.MEDIUM

    evidence_signatures = [b.signature for b in buys]

    return WalletProfile(
        wallet_address=clean_wallet,
        token_address=clean_token,
        first_buy_time=first_buy_time,
        first_buy_slot=first_buy_slot,
        first_buy_signature=first_buy_sig,
        first_buy_amount=first_buy_amount,
        time_after_launch=time_after_launch,
        total_buy_amount=total_buy_amount,
        buy_count=buy_count,
        sell_count=0,
        total_sell_amount=0.0,
        current_holding=current_holding,
        exit_ratio=0.0,
        holder_rank=holder_rank,
        holder_percentage=holder_percentage,
        score=0.0,
        confidence=confidence,
        is_same_block_sniper=False,
        evidence_signatures=evidence_signatures,
    )


def tag_same_block_snipers(profiles: List[WalletProfile]) -> List[WalletProfile]:
    """Identify and tag early buyers entering on the exact same Solana slot/block (Sniper bot clusters).
    
    If 2 or more wallets share the exact same first_buy_slot, their is_same_block_sniper
    flag is set to True.
    
    Args:
        profiles: List of constructed WalletProfile objects.
        
    Returns:
        The mutated/updated list of WalletProfile objects.
    """
    slot_counts: Dict[int, int] = {}
    for p in profiles:
        if p.first_buy_slot is not None:
            slot_counts[p.first_buy_slot] = slot_counts.get(p.first_buy_slot, 0) + 1

    for p in profiles:
        if p.first_buy_slot is not None and slot_counts[p.first_buy_slot] >= 2:
            p.is_same_block_sniper = True
        else:
            p.is_same_block_sniper = False

    return profiles

