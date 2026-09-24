"""Transaction detail collector module for EBRS.

Fetches on-chain transaction details via Solscan Pro API with SQLite-first caching,
parses balance changes and program IDs into Pydantic TransactionDetail models,
and atomically persists new transaction records.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Union

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

    # Extract block time and slot
    bt = data.get("block_time") or data.get("time") or data.get("blockTime") or 0
    try:
        block_time = int(bt)
    except (ValueError, TypeError):
        block_time = 0

    raw_slot = data.get("slot") or (raw_data.get("slot") if isinstance(raw_data, dict) else None)
    slot: Optional[int] = None
    if raw_slot is not None:
        try:
            slot = int(raw_slot)
        except (ValueError, TypeError):
            slot = None

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

            divisor = 10**decs_int if decs_int > 0 else 1

            # Priority A: Check if source explicitly indicates it is already a UI amount
            is_explicit_ui = (
                item.get("is_ui_amount") is True
                or item.get("ui_amount") is not None
                or item.get("uiAmount") is not None
                or item.get("uiAmountString") is not None
            )

            if is_explicit_ui:
                # Value is already in UI token units, do NOT divide by 10^decimals
                pre = float(item.get("pre_balance") or item.get("preBalance") or 0.0)
                post = float(item.get("post_balance") or item.get("postBalance") or 0.0)
                if item.get("ui_amount") is not None:
                    chg = float(item["ui_amount"])
                elif item.get("uiAmount") is not None:
                    chg = float(item["uiAmount"])
                elif item.get("uiAmountString") is not None:
                    try:
                        chg = float(item["uiAmountString"])
                    except (ValueError, TypeError):
                        chg = float(item.get("change") if item.get("change") is not None else (post - pre))
                elif item.get("change") is not None:
                    chg = float(item["change"])
                else:
                    chg = post - pre

            # Priority B: Check if source gives a raw integer amount (e.g. from Solscan API or raw events)
            elif item.get("raw_amount") is not None:
                raw_amt = float(item["raw_amount"])
                chg = raw_amt / divisor
                pre = float(item.get("pre_balance") or item.get("preBalance") or 0.0)
                if isinstance(item.get("pre_balance"), int) or (isinstance(item.get("pre_balance"), str) and item["pre_balance"].lstrip("-+").isdigit()):
                    pre = pre / divisor
                post = float(item.get("post_balance") or item.get("postBalance") or 0.0)
                if isinstance(item.get("post_balance"), int) or (isinstance(item.get("post_balance"), str) and item["post_balance"].lstrip("-+").isdigit()):
                    post = post / divisor

            elif item.get("amount") is not None:
                amt_val = item.get("amount")
                # If int or integer string without dot, it is raw amount
                if isinstance(amt_val, int) or (isinstance(amt_val, str) and str(amt_val).lstrip("-+").isdigit()):
                    raw_amt = float(amt_val)
                    chg = raw_amt / divisor
                else:
                    # Float or string with decimal point: already UI amount
                    chg = float(amt_val)
                pre = float(item.get("pre_balance") or item.get("preBalance") or 0.0)
                if isinstance(item.get("pre_balance"), int) or (isinstance(item.get("pre_balance"), str) and str(item["pre_balance"]).lstrip("-+").isdigit()):
                    pre = pre / divisor
                post = float(item.get("post_balance") or item.get("postBalance") or 0.0)
                if isinstance(item.get("post_balance"), int) or (isinstance(item.get("post_balance"), str) and str(item["post_balance"]).lstrip("-+").isdigit()):
                    post = post / divisor

            else:
                # Priority C: Standard pre/post/change handling
                # If pre_balance/post_balance/change are raw integers, divide; if already float with decimals, keep as UI amount
                pre_raw = item.get("pre_balance") or item.get("preBalance") or 0.0
                post_raw = item.get("post_balance") or item.get("postBalance") or 0.0
                chg_raw = item.get("change")

                pre_is_int = isinstance(pre_raw, int) or (isinstance(pre_raw, str) and pre_raw.lstrip("-+").isdigit())
                post_is_int = isinstance(post_raw, int) or (isinstance(post_raw, str) and post_raw.lstrip("-+").isdigit())
                chg_is_int = isinstance(chg_raw, int) or (isinstance(chg_raw, str) and str(chg_raw).lstrip("-+").isdigit())

                # If all non-zero inputs are pure integers, they are base units -> divide
                if (chg_is_int or chg_raw is None) and (pre_is_int or float(pre_raw) == 0.0) and (post_is_int or float(post_raw) == 0.0):
                    pre = float(pre_raw) / divisor
                    post = float(post_raw) / divisor
                    if chg_raw is not None:
                        chg = float(chg_raw) / divisor
                    else:
                        chg = post - pre
                else:
                    # Floating point represents UI amount directly
                    pre = float(pre_raw)
                    post = float(post_raw)
                    chg = float(chg_raw) if chg_raw is not None else (post - pre)

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
        slot=slot,
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
    client: Any,
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
