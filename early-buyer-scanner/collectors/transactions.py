"""Transaction detail collector module for EBRS.

Fetches on-chain transaction details via Solscan Pro API with SQLite-first caching,
parses balance changes and program IDs into Pydantic TransactionDetail models,
and atomically persists new transaction records.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from api.solscan import SolscanClient
from models.schemas import BalanceChange, TransactionDetail
from storage.database import Database

logger = logging.getLogger(__name__)


def parse_solscan_tx_detail(raw_data: Dict[str, Any], signature: str) -> TransactionDetail:
    """Parse Solscan transaction detail payload into a TransactionDetail model."""
    data: Dict[str, Any] = {}
    if isinstance(raw_data, dict):
        if "data" in raw_data and isinstance(raw_data["data"], dict):
            data = raw_data["data"]
        else:
            data = raw_data

    # Extract block time
    bt = data.get("block_time") or data.get("time") or data.get("blockTime") or 0
    try:
        block_time = int(bt)
    except (ValueError, TypeError):
        block_time = 0

    # Extract signer (string or list of strings)
    signer_val = data.get("signer")
    signer = ""
    if isinstance(signer_val, list) and signer_val:
        signer = str(signer_val[0]).strip()
    elif isinstance(signer_val, str):
        signer = signer_val.strip()

    # Extract status
    status_raw = data.get("status", "Success")
    if status_raw in (1, "1", True, "Success", "success"):
        status = "Success"
    else:
        status = "Failed" if status_raw in (0, "0", False, "Failed", "failed") else str(status_raw)

    # Extract priority fee / fee
    fee_val = data.get("priority_fee") or data.get("fee")
    priority_fee: Optional[float] = None
    if fee_val is not None:
        try:
            raw_fee = float(fee_val)
            # If fee is expressed in lamports (> 1000)
            priority_fee = raw_fee / 1e9 if raw_fee > 1000 else raw_fee
        except (ValueError, TypeError):
            priority_fee = None

    # Extract SOL balance changes
    sol_changes: List[BalanceChange] = []
    raw_sol_changes = (
        data.get("sol_bal_change")
        or data.get("sol_balance_change")
        or data.get("solBalanceChange")
        or []
    )
    if isinstance(raw_sol_changes, list):
        for item in raw_sol_changes:
            if not isinstance(item, dict):
                continue
            addr = (item.get("address") or item.get("account") or "").strip()
            pre = float(item.get("pre_balance") or item.get("preBalance") or 0.0)
            post = float(item.get("post_balance") or item.get("postBalance") or 0.0)
            chg = float(item.get("change") if item.get("change") is not None else (post - pre))

            # Normalize lamports to SOL if represented as large integers
            if abs(chg) >= 10000 or abs(pre) >= 1000000000 or abs(post) >= 1000000000:
                pre = pre / 1e9
                post = post / 1e9
                chg = chg / 1e9

            sol_changes.append(
                BalanceChange(
                    address=addr,
                    pre_balance=pre,
                    post_balance=post,
                    change=chg,
                    mint=None,
                    decimals=9,
                )
            )

    # Extract Token balance changes
    token_changes: List[BalanceChange] = []
    raw_token_changes = (
        data.get("token_bal_change")
        or data.get("token_balance_change")
        or data.get("tokenBalanceChange")
        or []
    )
    if isinstance(raw_token_changes, list):
        for item in raw_token_changes:
            if not isinstance(item, dict):
                continue
            addr = (item.get("address") or item.get("account") or "").strip()
            mint = (
                item.get("token_address")
                or item.get("mint")
                or item.get("tokenAddress")
                or ""
            ).strip()
            decs = item.get("decimals") or item.get("token_decimals") or 9
            try:
                decs_int = int(decs)
            except (ValueError, TypeError):
                decs_int = 9

            pre = float(item.get("pre_balance") or item.get("preBalance") or 0.0)
            post = float(item.get("post_balance") or item.get("postBalance") or 0.0)
            chg = float(item.get("change") if item.get("change") is not None else (post - pre))

            # If token change is in raw base units (> 10^decimals), convert to UI units
            divisor = 10**decs_int
            if decs_int > 0 and (abs(chg) >= divisor * 10 or abs(pre) >= divisor * 10 or abs(post) >= divisor * 10):
                pre = pre / divisor
                post = post / divisor
                chg = chg / divisor

            token_changes.append(
                BalanceChange(
                    address=addr,
                    pre_balance=pre,
                    post_balance=post,
                    change=chg,
                    mint=mint if mint else None,
                    decimals=decs_int,
                )
            )

    # Extract programs involved
    programs: List[str] = []
    raw_progs = (
        data.get("programs_involved")
        or data.get("programs")
        or data.get("program_ids")
        or data.get("programIds")
        or []
    )
    if isinstance(raw_progs, list):
        for p in raw_progs:
            if isinstance(p, str):
                programs.append(p.strip())
            elif isinstance(p, dict) and "program_id" in p:
                programs.append(str(p["program_id"]).strip())

    # Fallback for signer if empty
    if not signer and sol_changes:
        signer = sol_changes[0].address

    return TransactionDetail(
        signature=signature.strip(),
        block_time=block_time,
        signer=signer,
        sol_balance_changes=sol_changes,
        token_balance_changes=token_changes,
        programs=programs,
        status=status,
        priority_fee=priority_fee,
        raw_data=raw_data,
    )


def get_transaction_details_batch(
    signatures: List[str],
    client: SolscanClient,
    db: Database,
) -> List[TransactionDetail]:
    """Retrieve on-chain transaction details for a list of signatures.
    
    Adheres strictly to the SQLite-first caching requirement:
    - If the signature already exists in the transactions table with raw_data,
      it is parsed locally with zero network call.
    - If not in SQLite, it is fetched via SolscanClient, parsed, and atomically
      persisted to the transactions table.
    
    Args:
        signatures: List of Solana transaction signatures.
        client: SolscanClient instance.
        db: Database instance.
        
    Returns:
        List of parsed TransactionDetail models.
    """
    details: List[TransactionDetail] = []

    for sig in signatures:
        clean_sig = sig.strip()
        if not clean_sig:
            continue

        # 1. Check if signature exists in SQLite transactions table
        existing_tx = db.get_transaction(clean_sig)
        if existing_tx and existing_tx.get("raw_data"):
            logger.debug(f"Transaction {clean_sig} resolved from SQLite transactions table.")
            tx_detail = parse_solscan_tx_detail(existing_tx["raw_data"], clean_sig)
            details.append(tx_detail)
            continue

        # 2. Fetch from Solscan (which internally checks api_cache table as well)
        raw_resp = client.get_transaction_detail(clean_sig)

        # 3. Parse into TransactionDetail Pydantic model
        tx_detail = parse_solscan_tx_detail(raw_resp, clean_sig)
        details.append(tx_detail)

        # 4. Atomically persist to transactions table
        with db.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO transactions (
                    signature, token_address, block_time, wallet,
                    classification, confidence, sol_change, token_change,
                    programs, raw_data
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signature) DO UPDATE SET
                    block_time = excluded.block_time,
                    programs = excluded.programs,
                    raw_data = COALESCE(excluded.raw_data, transactions.raw_data);
                """,
                (
                    tx_detail.signature,
                    "",  # Set during classification in analyzer
                    tx_detail.block_time,
                    tx_detail.signer,
                    "UNKNOWN",
                    "UNKNOWN",
                    0.0,
                    0.0,
                    json.dumps(tx_detail.programs),
                    json.dumps(raw_resp),
                ),
            )

    return details
