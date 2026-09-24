"""Sell analyzer module for Early Buyer Scanner (EBRS).

Aggregates verified SELL transactions, tracks exit behavior,
and implements conservative bilateral lifecycle reconciliation (FR-10).
"""

from typing import List, Optional, Tuple

from models.schemas import (
    ClassificationEnum,
    ConfidenceEnum,
    TransactionClassification,
    WalletProfile,
)


def analyze_wallet_sells(
    wallet_address: str,
    tx_classifications: List[TransactionClassification],
    total_acquired: float,
) -> Tuple[int, float, Optional[int], float, float]:
    """Analyze sell transactions for a wallet and compute exit ratio.
    
    Args:
        wallet_address: Target wallet address.
        tx_classifications: List of all classified transactions for this wallet.
        total_acquired: Total tokens acquired via verified buys and valid inflows.
        
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
    sells.sort(key=lambda tx: tx.block_time or 0)

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


def reconcile_wallet_lifecycle(
    profile: WalletProfile,
    tx_classifications: List[TransactionClassification],
) -> None:
    """Validate lifecycle reconciliation invariants via bilateral conservation formula.
    
    Inflows:
        total_inflow = total_buy + transfer_in
    Valid Outflows:
        total_valid_outflow = total_sell + transfer_out + burn
    Residual:
        residual = total_inflow - total_valid_outflow - current_holding
        
    UNKNOWN outflows are NOT counted as valid outflows and are tracked separately.
    Bypass (total_accounted_exit >= total_buy * 0.999) is permanently removed.
    """
    clean_wallet = profile.wallet_address.strip()

    # 1. Inflows
    total_buy = profile.total_buy_amount
    transfer_in = sum(
        tx.token_change
        for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.token_change > 0 and tx.classification in (
            ClassificationEnum.TRANSFER_IN,
            ClassificationEnum.DISTRIBUTION,
            ClassificationEnum.TRANSFER,
        )
    )
    unknown_in = sum(
        tx.token_change
        for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.token_change > 0 and tx.classification == ClassificationEnum.UNKNOWN
    )

    # 2. Valid Outflows
    total_sell = profile.total_sell_amount
    transfer_out = sum(
        abs(tx.token_change)
        for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.token_change < 0 and tx.classification in (
            ClassificationEnum.TRANSFER_OUT,
            ClassificationEnum.TRANSFER,
        )
    )
    burn = sum(
        abs(tx.token_change)
        for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.token_change < 0 and tx.classification == ClassificationEnum.BURN
    )

    # 3. Unresolved Outflows (tracked separately, NEVER added to valid outflow)
    unknown_out = sum(
        abs(tx.token_change)
        for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.token_change < 0 and tx.classification == ClassificationEnum.UNKNOWN
    )
    unknown_tx_count = sum(
        1 for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.classification == ClassificationEnum.UNKNOWN and abs(tx.token_change) > 0.0
    )

    total_inflow = total_buy + transfer_in
    total_valid_outflow = total_sell + transfer_out + burn
    residual = total_inflow - total_valid_outflow - profile.current_holding

    # Store accounting metrics on profile
    profile.transfer_in_amount = transfer_in
    profile.transfer_out_amount = transfer_out
    profile.net_transfer_amount = transfer_in - transfer_out
    profile.burn_amount = burn
    profile.unknown_in_amount = unknown_in
    profile.unknown_outflow_amount = unknown_out
    profile.reconciliation_difference = residual
    profile.unknown_tx_count = unknown_tx_count

    # Tolerance: 1.0 token or 0.5% of total inflow
    tolerance = max(1.0, abs(total_inflow) * 0.005) if total_inflow > 0 else 1.0

    # Determine reconciliation status
    if profile.lifecycle_truncated or not profile.lifecycle_history_complete:
        profile.balance_reconciliation_status = "INCOMPLETE"
    elif abs(residual) <= tolerance and unknown_out == 0.0 and unknown_in == 0.0 and unknown_tx_count == 0:
        profile.balance_reconciliation_status = "MATCH"
    else:
        profile.balance_reconciliation_status = "MISMATCH"

    # Strict Confidence Evaluation (Evidence First)
    if (
        profile.first_buy_signature is not None
        and profile.balance_reconciliation_status == "MATCH"
        and profile.unknown_outflow_amount == 0.0
        and profile.unknown_in_amount == 0.0
        and profile.unknown_tx_count == 0
        and not profile.lifecycle_truncated
        and profile.lifecycle_history_complete
    ):
        # All 8 strict criteria met for HIGH
        profile.confidence = ConfidenceEnum.HIGH
    elif (
        profile.lifecycle_truncated
        or profile.balance_reconciliation_status == "MISMATCH"
        or profile.unknown_outflow_amount > tolerance
        or profile.unknown_tx_count > 0
    ):
        profile.confidence = ConfidenceEnum.LOW
    else:
        profile.confidence = ConfidenceEnum.MEDIUM


def enrich_profile_with_sells(
    profile: WalletProfile,
    tx_classifications: List[TransactionClassification],
) -> WalletProfile:
    """Enrich a WalletProfile with sell metrics, calculated exit ratio, and reconciliation."""
    clean_wallet = profile.wallet_address.strip()
    transfer_in = sum(
        tx.token_change
        for tx in tx_classifications
        if tx.wallet == clean_wallet and tx.token_change > 0 and tx.classification in (
            ClassificationEnum.TRANSFER_IN,
            ClassificationEnum.DISTRIBUTION,
            ClassificationEnum.TRANSFER,
        )
    )
    total_acquired = profile.total_buy_amount + transfer_in

    sell_count, total_sell_amount, _, _, exit_ratio = analyze_wallet_sells(
        wallet_address=profile.wallet_address,
        tx_classifications=tx_classifications,
        total_acquired=total_acquired,
    )

    profile.sell_count = sell_count
    profile.total_sell_amount = total_sell_amount
    profile.exit_ratio = exit_ratio

    reconcile_wallet_lifecycle(profile, tx_classifications)
    return profile
