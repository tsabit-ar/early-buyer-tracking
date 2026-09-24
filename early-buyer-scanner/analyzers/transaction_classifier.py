"""Transaction classification engine for Early Buyer Scanner (EBRS).

Implements FR-07, Section 10, and Section 23 ("Evidence First, Score Second") of the PRD:
- Structural transaction classification (BUY, SELL, TRANSFER_IN, TRANSFER_OUT, BURN, DISTRIBUTION, UNKNOWN).
- Micro-sell detection with economic exchange proof (regardless of small quote amounts).
- Separation of network fees from quote assets received.
- Guard against false SELLs on pure token transfers and non-swap router interactions.
"""

from typing import List, Optional, Tuple

from config import (
    BURN_ADDRESSES,
    KNOWN_DEX_PROGRAMS,
    QUOTE_ASSET_MINTS,
    USDC_MINT,
    USDT_MINT,
    WSOL_MINT,
)
from models.schemas import (
    ClassificationEnum,
    ConfidenceEnum,
    TransactionClassification,
    TransactionDetail,
)

# Fee threshold to prevent ordinary transfers from being misclassified as BUYs
# Coincides with standard network transaction fee + rent-exempt ATA initialization allowance
FEE_THRESHOLD_SOL: float = 0.003
FEE_THRESHOLD_USD: float = 0.50


def is_dex_swap(tx: TransactionDetail, wallet: str, token_mint: str) -> bool:
    """Checks whether the transaction involves a known DEX/AMM program or liquidity pool."""
    clean_token = token_mint.strip()
    clean_wallet = wallet.strip()

    # 1. Check known DEX/AMM programs
    for p in tx.programs:
        if p in KNOWN_DEX_PROGRAMS:
            return True

    # 2. Check known liquidity pools / bonding curves for target mint
    from analyzers.candidate_generator import get_token_pool_addresses
    pool_addresses = get_token_pool_addresses(clean_token)
    for tb in tx.token_balance_changes:
        if tb.address.strip() in pool_addresses and (tb.mint is None or tb.mint.strip() == clean_token):
            return True

    return False


def is_burn(tx: TransactionDetail, wallet: str, token_mint: str) -> bool:
    """Checks if the transaction represents a token burn (supply reduction or burn instruction)."""
    clean_wallet = wallet.strip()
    clean_token = token_mint.strip()

    wallet_token_chg = sum(
        tb.change for tb in tx.token_balance_changes
        if tb.address.strip() == clean_wallet and (tb.mint is None or tb.mint.strip() == clean_token)
    )
    if wallet_token_chg >= 0:
        return False

    # Check if there is NO recipient receiving the tokens
    total_token_increase = sum(
        tb.change for tb in tx.token_balance_changes
        if tb.address.strip() != clean_wallet and (tb.mint is None or tb.mint.strip() == clean_token) and tb.change > 0
    )

    # Check burn addresses
    has_burn_address = any(
        tb.address.strip() in BURN_ADDRESSES and tb.change > 0
        for tb in tx.token_balance_changes
        if tb.mint is None or tb.mint.strip() == clean_token
    )

    # Wallet cannot have received quote asset
    wallet_sol_chg = sum(sb.change for sb in tx.sol_balance_changes if sb.address.strip() == clean_wallet)
    if (total_token_increase <= 1e-6 or has_burn_address) and wallet_sol_chg <= 0.0001:
        return True

    return False


def is_token_sale(
    tx: TransactionDetail,
    wallet: str,
    token_mint: str,
    is_dex: bool,
    net_quote_change: float,
) -> bool:
    """Evaluates whether token outflow constitutes a verified SELL with economic exchange proof."""
    clean_wallet = wallet.strip()
    clean_token = token_mint.strip()

    wallet_token_chg = sum(
        tb.change for tb in tx.token_balance_changes
        if tb.address.strip() == clean_wallet and (tb.mint is None or tb.mint.strip() == clean_token)
    )
    # Must be token outflow
    if wallet_token_chg >= 0:
        return False

    # Check pool/bonding curve interaction
    from analyzers.candidate_generator import get_token_pool_addresses
    pool_addresses = get_token_pool_addresses(clean_token)
    pool_token_increase = sum(
        tb.change for tb in tx.token_balance_changes
        if tb.address.strip() in pool_addresses and (tb.mint is None or tb.mint.strip() == clean_token) and tb.change > 0
    )

    # 1. Economic exchange proof: wallet received quote asset net of gas fees
    # Micro-sell support: even small positive net quote (> 0.000001) proves sale
    quote_received = net_quote_change > 0.000001

    # 2. Explicit swap/sell action from parsed instructions
    explicit_swap_action = False
    if tx.raw_data:
        d = tx.raw_data.get("data", tx.raw_data)
        actions = str(d).lower()
        if "swap" in actions or "sell" in actions or "trade" in actions:
            explicit_swap_action = True

    # SELL requirement:
    # (DEX involved or pool absorbed tokens) AND (quote received or explicit swap action)
    if (is_dex or pool_token_increase > 0) and (quote_received or explicit_swap_action):
        return True

    return False


def is_transfer(
    tx: TransactionDetail,
    wallet: str,
    token_mint: str,
    is_dex: bool,
    net_quote_change: float,
) -> Tuple[bool, ClassificationEnum]:
    """Identifies peer-to-peer / inter-wallet transfer and determines direction (TRANSFER_IN / TRANSFER_OUT)."""
    clean_wallet = wallet.strip()
    clean_token = token_mint.strip()

    wallet_token_chg = sum(
        tb.change for tb in tx.token_balance_changes
        if tb.address.strip() == clean_wallet and (tb.mint is None or tb.mint.strip() == clean_token)
    )
    if wallet_token_chg == 0:
        return False, ClassificationEnum.UNKNOWN

    # If it's a confirmed sale, it's not a transfer
    if is_token_sale(tx, wallet, token_mint, is_dex, net_quote_change):
        return False, ClassificationEnum.UNKNOWN

    # If it's a burn, not a transfer
    if is_burn(tx, wallet, token_mint):
        return False, ClassificationEnum.UNKNOWN

    # Check quote asset delta: for transfers, delta should only be network gas fee (or 0)
    is_gas_only = abs(net_quote_change) <= FEE_THRESHOLD_SOL

    # Inflow (token received)
    if wallet_token_chg > 0:
        # If recipient spent zero gas and no DEX, it's DISTRIBUTION (or TRANSFER_IN)
        if net_quote_change >= -0.00005 and not is_dex:
            return True, ClassificationEnum.DISTRIBUTION
        elif is_gas_only:
            return True, ClassificationEnum.TRANSFER_IN

    # Outflow (token sent)
    elif wallet_token_chg < 0:
        # Check if another account received the tokens
        has_recipient = any(
            tb.change > 0 and tb.address.strip() != clean_wallet
            for tb in tx.token_balance_changes
            if tb.mint is None or tb.mint.strip() == clean_token
        )
        if has_recipient and is_gas_only:
            return True, ClassificationEnum.TRANSFER_OUT

    return False, ClassificationEnum.UNKNOWN


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
            slot=tx.slot,
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

    # 3. Compute Net Delta for Quote Assets (SOL, WSOL, USDC, USDT)
    sol_change: float = 0.0
    for sb in tx.sol_balance_changes:
        if sb.address.strip() == clean_wallet:
            sol_change += sb.change

    wsol_change: float = 0.0
    usdc_change: float = 0.0
    usdt_change: float = 0.0
    for tb in tx.token_balance_changes:
        if tb.address.strip() == clean_wallet and tb.mint:
            mint_addr = tb.mint.strip()
            if mint_addr == WSOL_MINT:
                wsol_change += tb.change
            elif mint_addr == USDC_MINT:
                usdc_change += tb.change
            elif mint_addr == USDT_MINT:
                usdt_change += tb.change

    sol_quote_change: float = sol_change + wsol_change
    usd_quote_change: float = usdc_change + usdt_change

    # Determine primary quote asset change
    if abs(usd_quote_change) > abs(sol_quote_change) and abs(usd_quote_change) > FEE_THRESHOLD_USD:
        net_quote_change = usd_quote_change
        quote_symbol = "USDC/USDT"
        is_quote_spent = usd_quote_change < -FEE_THRESHOLD_USD
    else:
        net_quote_change = sol_quote_change
        quote_symbol = "SOL/WSOL"
        is_quote_spent = sol_quote_change < -FEE_THRESHOLD_SOL

    # 4. Check DEX/AMM Program Involvement
    has_dex = is_dex_swap(tx, clean_wallet, clean_token)
    detected_dex = [p for p in tx.programs if p in KNOWN_DEX_PROGRAMS]

    if has_dex:
        reasons.append(f"DEX/AMM program detected: {', '.join(detected_dex) if detected_dex else 'Pool Interaction'}")

    # 4b. Pool and Counterparty Guards
    from analyzers.candidate_generator import get_token_pool_addresses
    pool_addresses = get_token_pool_addresses(clean_token)
    if clean_wallet in pool_addresses:
        reasons.append(f"Wallet is a known liquidity pool/bonding curve PDA ({clean_wallet})")
        reasons.append("Pools are liquidity counterparties, not early buyers")
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            slot=tx.slot,
            classification=ClassificationEnum.UNKNOWN,
            confidence=ConfidenceEnum.LOW,
            reasons=reasons,
            sol_change=net_quote_change,
            token_change=token_change,
            programs=detected_dex if has_dex else tx.programs,
        )

    # In a DEX swap, if the signer sold target tokens, any other wallet receiving tokens is a counterparty pool
    if has_dex and tx.signer and clean_wallet != tx.signer:
        signer_token_change = sum(
            tb.change for tb in tx.token_balance_changes
            if tb.address.strip() == tx.signer.strip() and (tb.mint is None or tb.mint.strip() == clean_token)
        )
        signer_sol_change = sum(
            sb.change for sb in tx.sol_balance_changes
            if sb.address.strip() == tx.signer.strip()
        )
        if signer_token_change < 0 and signer_sol_change > 0:
            reasons.append(f"Transaction is a SELL by signer {tx.signer}")
            reasons.append(f"Evaluated wallet {clean_wallet} is counterparty/pool absorbing tokens")
            return TransactionClassification(
                signature=tx.signature,
                wallet=clean_wallet,
                token_address=clean_token,
                block_time=tx.block_time,
                slot=tx.slot,
                classification=ClassificationEnum.UNKNOWN,
                confidence=ConfidenceEnum.LOW,
                reasons=reasons,
                sol_change=net_quote_change,
                token_change=token_change,
                programs=detected_dex if has_dex else tx.programs,
            )

    # 5. Core Structural Classification Logic (Evidence First)

    # Case A: BUY (Token Received + Significant Quote Asset Spent)
    if token_change > 0 and is_quote_spent:
        reasons.append(f"Target token acquired: +{token_change:,.4f}")
        reasons.append(f"Quote asset spent: {net_quote_change:.6f} {quote_symbol}")
        confidence = ConfidenceEnum.HIGH if has_dex else ConfidenceEnum.MEDIUM
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            slot=tx.slot,
            classification=ClassificationEnum.BUY,
            confidence=confidence,
            reasons=reasons,
            sol_change=net_quote_change,
            token_change=token_change,
            programs=detected_dex if has_dex else tx.programs,
        )

    # Case B: SELL (Token Outflow + Valid Swap Economic Proof)
    if is_token_sale(tx, clean_wallet, clean_token, has_dex, net_quote_change):
        reasons.append(f"Target token sold: {token_change:,.4f}")
        reasons.append(f"Quote asset received: +{net_quote_change:.6f} {quote_symbol}")
        confidence = ConfidenceEnum.HIGH if has_dex else ConfidenceEnum.MEDIUM
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            slot=tx.slot,
            classification=ClassificationEnum.SELL,
            confidence=confidence,
            reasons=reasons,
            sol_change=net_quote_change,
            token_change=token_change,
            programs=detected_dex if has_dex else tx.programs,
        )

    # Case C: BURN
    if is_burn(tx, clean_wallet, clean_token):
        reasons.append(f"Token burn detected: {token_change:,.4f}")
        reasons.append("Token destroyed with no economic exchange")
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            slot=tx.slot,
            classification=ClassificationEnum.BURN,
            confidence=ConfidenceEnum.HIGH,
            reasons=reasons,
            sol_change=net_quote_change,
            token_change=token_change,
            programs=tx.programs,
        )

    # Case D: TRANSFER (P2P / Inter-wallet Transfer In or Out)
    is_tr, tr_type = is_transfer(tx, clean_wallet, clean_token, has_dex, net_quote_change)
    if is_tr:
        reasons.append(f"Token transfer detected: {token_change:,.4f} ({tr_type.value})")
        reasons.append(f"Quote delta ({net_quote_change:.6f} SOL) is within gas fee limit ({FEE_THRESHOLD_SOL} SOL)")
        return TransactionClassification(
            signature=tx.signature,
            wallet=clean_wallet,
            token_address=clean_token,
            block_time=tx.block_time,
            slot=tx.slot,
            classification=tr_type,
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
        slot=tx.slot,
        classification=ClassificationEnum.UNKNOWN,
        confidence=ConfidenceEnum.LOW,
        reasons=reasons,
        sol_change=net_quote_change,
        token_change=token_change,
        programs=tx.programs,
    )
