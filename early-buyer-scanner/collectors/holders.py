"""Token holders collector module for Early Buyer Scanner (EBRS).

Fetches top token holders via Solscan Pro API with SQLite-first caching
and returns a structured lookup map: wallet_address -> {amount, rank, percentage, value_usd}.
"""

import logging
from typing import Any, Dict, List, Optional

from api.solscan import SolscanClient
from collectors.token import validate_solana_address
from storage.database import Database

logger = logging.getLogger(__name__)


def get_token_holders_data(
    mint_address: str,
    client: SolscanClient,
    db: Database,
    page: int = 1,
    page_size: int = 100,
) -> Dict[str, Dict[str, Any]]:
    """Retrieve top token holders and format as a wallet address lookup map.
    
    Args:
        mint_address: Solana token mint address.
        client: SolscanClient instance.
        db: Database instance.
        page: Page number (default: 1).
        page_size: Number of holders per page (default: 100).
        
    Returns:
        Dict mapping wallet_address -> {amount, rank, percentage, value_usd}.
    """
    valid_mint = validate_solana_address(mint_address)
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

    holders_map: Dict[str, Dict[str, Any]] = {}

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
