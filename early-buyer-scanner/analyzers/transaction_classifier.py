"""Transaction classification engine for Early Buyer Scanner (EBRS).

Implements FR-07, Section 10, and Section 23 ("Evidence First, Score Second") of the PRD:
- Analyzes net target token delta and quote asset (SOL/WSOL) delta.
- Identifies DEX/AMM program involvement.
- Classifies into BUY, SELL, TRANSFER, DISTRIBUTION, or UNKNOWN with strict confidence levels.
"""

from typing import List

from config import KNOWN_DEX_PROGRAMS, WSOL_MINT
from models.schemas import (
    ClassificationEnum,
    ConfidenceEnum,
    TransactionClassification,
    TransactionDetail,
)

# Fee threshold to prevent ordinary transfers from being misclassified as BUYs
# Coincides with standard network transaction fee + rent-exempt ATA initialization allowance
FEE_THRESHOLD_SOL: float = 0.003


def classify_transaction(
    tx: TransactionDetail,
    wallet_address: str,
    token_address: str,
) -> TransactionClassification:
    """Classify a transaction from the perspective of a specific wallet and token.
    
    Args:
        tx: Parsed TransactionDetail model.
        wallet_address: Target wallet address.
        token_address: Target token mint address.
        
    Returns:
        TransactionClassification with classification, confidence, reasons, and balance changes.
    """
    clean_wallet = wallet_address.strip()
    clean_token = token_address.strip()
    reasons: List[str] = []

    # 1. Verification of Transaction Execution Status
    if tx.status.lower() not in ("success", "1", "ok"):
        reasons.append(f"Transaction failed with status '{tx.status}'")
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            classification=ClassificationEnum.UNKNOWN,
            confidence=ConfidenceEnum.UNKNOWN,
            reasons=reasons,
            sol_change=0.0,
            token_change=0.0,
            programs=tx.programs,
        )

    # 2. Compute Net Delta for Target Token
    token_change: float = 0.0
    for tb in tx.token_balance_changes:
        if tb.address.strip() == clean_wallet:
            # Match token mint address or default token
            if tb.mint is None or tb.mint.strip() == clean_token:
                token_change += tb.change

    # 3. Compute Net Delta for Quote Asset (SOL + WSOL)
    sol_change: float = 0.0
    for sb in tx.sol_balance_changes:
        if sb.address.strip() == clean_wallet:
            sol_change += sb.change

    wsol_change: float = 0.0
    for tb in tx.token_balance_changes:
        if tb.address.strip() == clean_wallet and tb.mint and tb.mint.strip() == WSOL_MINT:
            wsol_change += tb.change

    net_quote_change: float = sol_change + wsol_change

    # 4. Check DEX/AMM Program Involvement
    detected_dex = [p for p in tx.programs if p in KNOWN_DEX_PROGRAMS]
    has_dex = len(detected_dex) > 0

    if has_dex:
        reasons.append(f"DEX/AMM program detected: {', '.join(detected_dex)}")

    # 5. Core Classification Logic (Evidence First)

    # Case A: BUY (Token Received + Significant Quote Asset Spent)
    if token_change > 0 and net_quote_change < -FEE_THRESHOLD_SOL:
        reasons.append(f"Target token acquired: +{token_change:,.4f}")
        reasons.append(
            f"Quote asset spent: {net_quote_change:.6f} SOL/WSOL "
            f"(exceeds threshold -{FEE_THRESHOLD_SOL} SOL)"
        )
        confidence = ConfidenceEnum.HIGH if has_dex else ConfidenceEnum.MEDIUM
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            classification=ClassificationEnum.BUY,
            confidence=confidence,
            reasons=reasons,
            sol_change=net_quote_change,
            token_change=token_change,
            programs=detected_dex if has_dex else tx.programs,
        )

    # Case B: SELL (Token Sent Out + Quote Asset Received)
    if token_change < 0 and net_quote_change > 0.001:
        reasons.append(f"Target token sold: {token_change:,.4f}")
        reasons.append(f"Quote asset received: +{net_quote_change:.6f} SOL/WSOL")
        confidence = ConfidenceEnum.HIGH if has_dex else ConfidenceEnum.MEDIUM
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            classification=ClassificationEnum.SELL,
            confidence=confidence,
            reasons=reasons,
            sol_change=net_quote_change,
            token_change=token_change,
            programs=detected_dex if has_dex else tx.programs,
        )

    # Case C: DISTRIBUTION (Token Received + Zero Quote Asset Spent + No DEX)
    # Recipient spent zero quote asset (gas was paid by distributor / authority)
    if token_change > 0 and net_quote_change >= -0.00005 and not has_dex:
        reasons.append(f"Target token received: +{token_change:,.4f}")
        reasons.append("No quote asset spent by recipient wallet")
        reasons.append("No DEX program involved (airdrop/mint/authority distribution)")
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            classification=ClassificationEnum.DISTRIBUTION,
            confidence=ConfidenceEnum.HIGH,
            reasons=reasons,
            sol_change=net_quote_change,
            token_change=token_change,
            programs=tx.programs,
        )

    # Case D: TRANSFER (Token Changed + Gas-only Quote Delta + No DEX)
    if token_change != 0 and abs(net_quote_change) <= FEE_THRESHOLD_SOL and not has_dex:
        reasons.append(f"Token transfer detected: {token_change:,.4f}")
        reasons.append(
            f"Quote asset delta ({net_quote_change:.6f} SOL) is within gas fee limit ({FEE_THRESHOLD_SOL} SOL)"
        )
        reasons.append("No DEX swap instruction detected")
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            classification=ClassificationEnum.TRANSFER,
            confidence=ConfidenceEnum.HIGH,
            reasons=reasons,
            sol_change=net_quote_change,
            token_change=token_change,
            programs=tx.programs,
        )

    # Case E: UNKNOWN (Ambiguous, No Balance Changes, or Contradictory Flow)
    reasons.append("Ambiguous on-chain balance movements or lack of economic exchange proof")
    reasons.append(f"Token delta: {token_change:,.4f}, Quote delta: {net_quote_change:.6f}")
    return TransactionClassification(
        signature=tx.signature,
        wallet=clean_wallet,
        token_address=clean_token,
        block_time=tx.block_time,
        classification=ClassificationEnum.UNKNOWN,
        confidence=ConfidenceEnum.LOW,
        reasons=reasons,
        sol_change=net_quote_change,
        token_change=token_change,
        programs=tx.programs,
    )
