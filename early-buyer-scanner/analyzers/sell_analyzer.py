"""Sell analyzer module for Early Buyer Scanner (EBRS).

Aggregates verified SELL transactions, tracks exit behavior,
and calculates the exit ratio with strict zero-division protection (FR-10).
"""

from typing import List, Optional, Tuple

from models.schemas import ClassificationEnum, TransactionClassification, WalletProfile


def analyze_wallet_sells(
    wallet_address: str,
    tx_classifications: List[TransactionClassification],
    total_acquired: float,
) -> Tuple[int, float, Optional[int], float, float]:
    """Analyze sell transactions for a wallet and compute exit ratio.
    
    Args:
        wallet_address: Target wallet address.
        tx_classifications: List of all classified transactions for this wallet.
        total_acquired: Total tokens acquired via verified buys.
        
    Returns:
        Tuple of (sell_count, total_sell_amount, first_sell_time, estimated_exit_amount, exit_ratio).
    """
    clean_wallet = wallet_address.strip()

    # Filter verified SELL transactions
    sells = [
        tx for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.classification == ClassificationEnum.SELL
    ]

    sell_count = len(sells)
    if sell_count == 0:
        return 0, 0.0, None, 0.0, 0.0

    # Sort chronologically
    sells.sort(key=lambda tx: tx.block_time)

    first_sell_time = sells[0].block_time
    total_sell_amount = sum(abs(s.token_change) for s in sells)
    estimated_exit_amount = total_sell_amount

    # Division by zero protection
    if total_acquired <= 0.0:
        exit_ratio = 0.0
    else:
        exit_ratio = estimated_exit_amount / total_acquired

    return (
        sell_count,
        total_sell_amount,
        first_sell_time,
        estimated_exit_amount,
        max(0.0, round(exit_ratio, 6)),
    )


def enrich_profile_with_sells(
    profile: WalletProfile,
    tx_classifications: List[TransactionClassification],
) -> WalletProfile:
    """Enrich a WalletProfile with sell metrics and calculated exit ratio."""
    sell_count, total_sell_amount, _, _, exit_ratio = analyze_wallet_sells(
        wallet_address=profile.wallet_address,
        tx_classifications=tx_classifications,
        total_acquired=profile.total_buy_amount,
    )

    profile.sell_count = sell_count
    profile.total_sell_amount = total_sell_amount
    profile.exit_ratio = exit_ratio
    return profile
