"""Token holders collector module for Early Buyer Scanner (EBRS).

Fetches top token holders via Solana RPC (getTokenLargestAccounts) or Solscan Pro API
with SQLite-first caching and returns a structured lookup map:
wallet_address -> {amount, rank, percentage, value_usd}.
"""

import logging
from typing import Any, Dict, List, Optional, Union

from collectors.token import validate_solana_address
from storage.database import Database

logger = logging.getLogger(__name__)


def get_token_holders_data(
    mint_address: str,
    client: Any,
    db: Database,
    page: int = 1,
    page_size: int = 100,
) -> Dict[str, Dict[str, Any]]:
    """Retrieve top token holders and format as a wallet address lookup map.
    
    Supports both SolanaRpcClient (via getTokenLargestAccounts + batch owner resolution)
    and SolscanClient (via get_token_holders).
    
    Args:
        mint_address: Solana token mint address.
        client: SolanaRpcClient or SolscanClient instance.
        db: Database instance.
        page: Page number (default: 1, for Solscan).
        page_size: Number of holders per page (default: 100, for Solscan).
        
    Returns:
        Dict mapping wallet_address -> {amount, rank, percentage, value_usd}.
    """
    valid_mint = validate_solana_address(mint_address)
    holders_map: Dict[str, Dict[str, Any]] = {}

    # 1. Native Solana RPC support (getTokenLargestAccounts)
    if hasattr(client, "get_token_largest_accounts"):
        try:
            largest_accounts = client.get_token_largest_accounts(valid_mint)
            if isinstance(largest_accounts, list):
                # Fetch total supply for percentage calculation
                total_supply = 0.0
                if hasattr(client, "get_token_supply"):
                    supply_info = client.get_token_supply(valid_mint)
                    if isinstance(supply_info, dict):
                        try:
                            total_supply = float(
                                supply_info.get("uiAmount")
                                or supply_info.get("amount")
                                or 0.0
                            )
                        except (ValueError, TypeError):
                            total_supply = 0.0

                # Extract token account addresses to resolve owners
                token_acc_addrs = [
                    acc.get("address")
                    for acc in largest_accounts
                    if isinstance(acc, dict) and acc.get("address")
                ]

                # Resolve owners using get_multiple_accounts if available
                owners_by_token_acc: Dict[str, str] = {}
                if hasattr(client, "get_multiple_accounts") and token_acc_addrs:
                    try:
                        acc_infos = client.get_multiple_accounts(token_acc_addrs)
                        for t_addr, info in zip(token_acc_addrs, acc_infos):
                            if isinstance(info, dict):
                                data_block = info.get("data")
                                if isinstance(data_block, dict):
                                    parsed = data_block.get("parsed")
                                    if isinstance(parsed, dict):
                                        owner = parsed.get("info", {}).get("owner")
                                        if owner:
                                            owners_by_token_acc[t_addr] = owner
                    except Exception as exc:
                        logger.warning(f"Could not batch resolve token account owners: {exc}")

                for idx, item in enumerate(largest_accounts, start=1):
                    if not isinstance(item, dict):
                        continue

                    t_addr = (item.get("address") or "").strip()
                    if not t_addr:
                        continue

                    owner_addr = owners_by_token_acc.get(t_addr, t_addr)

                    raw_amount = item.get("uiAmount")
                    if raw_amount is None:
                        raw_amount = item.get("amount") or 0.0
                    try:
                        amount = float(raw_amount)
                    except (ValueError, TypeError):
                        amount = 0.0

                    percentage = (amount / total_supply * 100.0) if total_supply > 0 else 0.0

                    holder_entry = {
                        "amount": amount,
                        "rank": idx,
                        "percentage": percentage,
                        "value_usd": 0.0,
                    }

                    # Index by both owner address and token account address
                    holders_map[owner_addr] = holder_entry
                    if t_addr != owner_addr:
                        holders_map[t_addr] = holder_entry

                return holders_map
        except Exception as exc:
            logger.warning(
                f"RPC get_token_largest_accounts encountered error: {exc}. "
                "Attempting fallback to get_token_holders if available."
            )

    # 2. Solscan Client fallback
    if hasattr(client, "get_token_holders"):
        raw_response = client.get_token_holders(valid_mint, page=page, page_size=page_size)

        items: List[Dict[str, Any]] = []
        if isinstance(raw_response, dict):
            if "data" in raw_response:
                data_val = raw_response["data"]
                if isinstance(data_val, list):
                    items = data_val
                elif isinstance(data_val, dict) and "items" in data_val:
                    items = data_val["items"]
        elif isinstance(raw_response, list):
            items = raw_response

        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue

            addr = (
                item.get("address")
                or item.get("owner")
                or item.get("wallet")
                or item.get("account")
                or ""
            ).strip()
            if not addr:
                continue

            raw_amount = item.get("amount") or item.get("value") or 0.0
            try:
                amount = float(raw_amount)
            except (ValueError, TypeError):
                amount = 0.0

            raw_rank = item.get("rank")
            try:
                rank = int(raw_rank) if raw_rank is not None else idx
            except (ValueError, TypeError):
                rank = idx

            raw_pct = item.get("percentage") or item.get("percent") or 0.0
            try:
                percentage = float(raw_pct)
            except (ValueError, TypeError):
                percentage = 0.0

            raw_usd = item.get("value_usd") or item.get("valueUsd") or item.get("usd_value") or 0.0
            try:
                value_usd = float(raw_usd)
            except (ValueError, TypeError):
                value_usd = 0.0

            holders_map[addr] = {
                "amount": amount,
                "rank": rank,
                "percentage": percentage,
                "value_usd": value_usd,
            }

    return holders_map
