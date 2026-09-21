"""Wallet funding analyzer and Sybil detection engine for EBRS.

Implements V2 Wallet Intelligence & Sybil Detection (PRD Section 18):
1. Traces wallet genesis to detect initial SOL funding source (Inflow Extraction).
2. Calculates wallet age relative to token launch and identifies fresh burner wallets (< 24h).
3. Identifies known CEX hot wallets (Binance, Coinbase, OKX, Bybit, MEXC, KuCoin, etc.).
4. Implements bounded pagination (depth limit) to prevent RPC draining on mature wallets.
5. Groups wallets sharing common EOA funders (> 0.05 SOL) into Sybil clusters.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from config import (
    KNOWN_CEX_WALLETS,
    MATURE_WALLET_AGE_DAYS,
    MAX_FUNDING_PAGES,
    MIN_CLUSTER_FUNDING_SOL,
    SYSTEM_PROGRAM_ID,
    settings,
)
from models.schemas import WalletProfile
from storage.database import Database

logger = logging.getLogger(__name__)


def trace_wallet_funder(
    wallet_address: str,
    rpc_client: Any,
    db: Optional[Database] = None,
    launch_time: Optional[int] = None,
) -> Tuple[Optional[str], str, Optional[float], Optional[float], Optional[str]]:
    """Trace a wallet's initial SOL funding source and age relative to token launch.

    Implements safety clauses:
    - Bounded pagination (depth limit = MAX_FUNDING_PAGES)
    - Early exit for mature wallets (> MATURE_WALLET_AGE_DAYS old)
    - Inflow balance change extraction for true funder identification

    Args:
        wallet_address: Target Solana wallet public key.
        rpc_client: SolanaRpcClient or compatible client.
        db: Optional Database instance for caching.
        launch_time: Unix timestamp of token launch.

    Returns:
        Tuple of:
        - funder_address: Address that initially funded the wallet (or None).
        - funder_type: "CEX", "EOA", "INTERNAL", "MATURE_WALLET", or "UNKNOWN".
        - wallet_age_days: Age in days at token launch (or None).
        - funding_amount_sol: Inflow amount received (or None).
        - funding_signature: Transaction signature of genesis/funding (or None).
    """
    if not wallet_address:
        return None, "UNKNOWN", None, None, None

    active_db = db or Database(settings.sqlite_db_path)

    # 1. Bounded pagination backwards to locate genesis transaction
    before: Optional[str] = None
    oldest_sig_info: Optional[Dict[str, Any]] = None
    pages_fetched = 0

    if not hasattr(rpc_client, "get_signatures_for_address"):
        return None, "UNKNOWN", None, None, None

    try:
        while pages_fetched < MAX_FUNDING_PAGES:
            batch = rpc_client.get_signatures_for_address(wallet_address, limit=1000, before=before)
            pages_fetched += 1

            if not batch:
                break

            oldest_in_page = batch[-1]
            oldest_sig_info = oldest_in_page
            bt = oldest_in_page.get("blockTime")

            # Check mature wallet early termination:
            # If oldest transaction in this page is already older than threshold, stop pagination!
            if launch_time and bt:
                age_days = (launch_time - bt) / 86400.0
                if age_days >= MATURE_WALLET_AGE_DAYS:
                    logger.debug(
                        f"Wallet {wallet_address[:8]} is mature (> {MATURE_WALLET_AGE_DAYS}d old at page {pages_fetched}). "
                        "Terminating funding pagination early."
                    )
                    return None, "MATURE_WALLET", round(age_days, 2), None, oldest_in_page.get("signature")

            if len(batch) < 1000:
                # Reached absolute genesis of this wallet
                break

            before = oldest_in_page.get("signature")

        # If we exhausted MAX_FUNDING_PAGES without reaching genesis, consider it mature
        if pages_fetched >= MAX_FUNDING_PAGES and oldest_sig_info:
            bt = oldest_sig_info.get("blockTime")
            age_days = ((launch_time - bt) / 86400.0) if (launch_time and bt) else None
            return None, "MATURE_WALLET", (round(age_days, 2) if age_days else None), None, oldest_sig_info.get("signature")

    except Exception as exc:
        logger.warning(f"Error querying signatures for wallet {wallet_address}: {exc}")
        return None, "UNKNOWN", None, None, None

    if not oldest_sig_info:
        return None, "UNKNOWN", None, None, None

    genesis_sig = oldest_sig_info.get("signature")
    first_tx_time = oldest_sig_info.get("blockTime")

    # 2. Compute wallet age
    wallet_age_days: Optional[float] = None
    if first_tx_time:
        ref_time = launch_time if (launch_time and launch_time > 0) else int(time.time())
        age_seconds = max(0, ref_time - first_tx_time)
        wallet_age_days = round(age_seconds / 86400.0, 3)

    if not genesis_sig:
        return None, "UNKNOWN", wallet_age_days, None, None

    # 3. Inflow Extraction: Parse genesis transaction to identify the funder
    funder_address: Optional[str] = None
    funder_type: str = "UNKNOWN"
    funding_amount_sol: Optional[float] = None

    try:
        raw_tx = rpc_client.get_transaction_detail(genesis_sig)
        data = raw_tx.get("data", {}) if isinstance(raw_tx, dict) else {}
        if not data and isinstance(raw_tx, dict):
            data = raw_tx

        sol_changes = (
            data.get("sol_bal_change")
            or data.get("sol_balance_change")
            or data.get("solBalanceChange")
            or []
        )

        incoming_sol = 0.0
        target_found = False

        if isinstance(sol_changes, list):
            # Check target wallet balance increase
            for sc in sol_changes:
                if not isinstance(sc, dict):
                    continue
                addr = (sc.get("address") or sc.get("account") or "").strip()
                if addr == wallet_address:
                    pre = float(sc.get("pre_balance") or sc.get("preBalance") or 0.0)
                    post = float(sc.get("post_balance") or sc.get("postBalance") or 0.0)
                    chg = float(sc.get("change") if sc.get("change") is not None else (post - pre))

                    # Normalize lamports to SOL if large integer
                    if abs(chg) >= 10000 or abs(pre) >= 1e9 or abs(post) >= 1e9:
                        chg /= 1e9

                    if chg > 0:
                        incoming_sol = chg
                        target_found = True
                        funding_amount_sol = round(incoming_sol, 4)
                        break

            # Find the sender account whose balance decreased
            if target_found and incoming_sol > 0:
                sender_candidates = []
                for sc in sol_changes:
                    if not isinstance(sc, dict):
                        continue
                    addr = (sc.get("address") or sc.get("account") or "").strip()
                    if addr == wallet_address or addr == SYSTEM_PROGRAM_ID:
                        continue

                    pre = float(sc.get("pre_balance") or sc.get("preBalance") or 0.0)
                    post = float(sc.get("post_balance") or sc.get("postBalance") or 0.0)
                    chg = float(sc.get("change") if sc.get("change") is not None else (post - pre))

                    if abs(chg) >= 10000 or abs(pre) >= 1e9 or abs(post) >= 1e9:
                        chg /= 1e9

                    if chg < 0:
                        sender_candidates.append((addr, abs(chg)))

                if sender_candidates:
                    # Pick sender with largest decrease (most likely funder)
                    sender_candidates.sort(key=lambda x: x[1], reverse=True)
                    funder_address = sender_candidates[0][0]

        # If not found via sol_bal_change, fallback to fee payer / signer
        if not funder_address:
            signers = data.get("signer", [])
            if isinstance(signers, list) and signers:
                top_signer = str(signers[0]).strip()
                if top_signer != wallet_address:
                    funder_address = top_signer
                else:
                    # Self-signed genesis without incoming transfer
                    funder_address = wallet_address
                    funder_type = "INTERNAL"
            elif isinstance(signers, str) and signers:
                if signers != wallet_address:
                    funder_address = signers
                else:
                    funder_address = wallet_address
                    funder_type = "INTERNAL"

        # 4. Classify funder type
        if funder_address:
            if funder_address in KNOWN_CEX_WALLETS:
                funder_type = "CEX"
            elif funder_address == wallet_address:
                funder_type = "INTERNAL"
            elif funder_type != "INTERNAL":
                funder_type = "EOA"

    except Exception as exc:
        logger.warning(f"Error parsing genesis tx {genesis_sig} for {wallet_address}: {exc}")

    return funder_address, funder_type, wallet_age_days, funding_amount_sol, genesis_sig


def detect_funder_clusters(
    profiles: List[WalletProfile],
    min_funding_amount: float = MIN_CLUSTER_FUNDING_SOL,
) -> List[WalletProfile]:
    """Cluster wallets funded by the same EOA address into Sybil clusters.

    Safety & precision rules:
    - Never cluster CEX funded wallets (CEX funds thousands of unrelated users).
    - Requires at least 2 distinct early buyer profiles with the exact same EOA funder.
    - Requires reasonable initial funding transfer (> min_funding_amount SOL, default 0.05 SOL).

    Args:
        profiles: List of WalletProfile models.
        min_funding_amount: Minimum SOL transfer threshold.

    Returns:
        Updated list of WalletProfile models with cluster_id populated where applicable.
    """
    if not profiles or len(profiles) < 2:
        return profiles

    # Group eligible profiles by non-CEX funder address
    funder_groups: Dict[str, List[WalletProfile]] = {}

    for p in profiles:
        # Exclude CEX, MATURE_WALLET, INTERNAL, UNKNOWN
        if p.funder_type != "EOA" or not p.funder_address:
            continue

        # Exclude known CEX wallets (double safety check)
        if p.funder_address in KNOWN_CEX_WALLETS:
            continue

        # Check funding amount threshold if present
        if p.funding_amount_sol is not None and p.funding_amount_sol < min_funding_amount:
            continue

        funder_groups.setdefault(p.funder_address, []).append(p)

    # Assign cluster IDs to groups with 2 or more wallets
    cluster_counter = 1
    for funder, group in sorted(funder_groups.items(), key=lambda x: len(x[1]), reverse=True):
        if len(group) >= 2:
            cluster_name = f"CLUSTER_{cluster_counter}"
            for p in group:
                p.cluster_id = cluster_name
            cluster_counter += 1
        else:
            for p in group:
                p.cluster_id = None

    return profiles
