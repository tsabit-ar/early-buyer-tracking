"""Historical token transfers collector module for EBRS.

Fetches chronological token transfers using pagination, converts raw Solscan
payloads into Pydantic TransferEvent models, and persists them into SQLite.
"""

import logging
from typing import Any, Dict, List, Optional

from api.solscan import SolscanClient
from collectors.token import validate_solana_address
from config import settings
from models.schemas import TransferEvent
from storage.database import Database

logger = logging.getLogger(__name__)


def _parse_transfer_item(item: Dict[str, Any], default_token: str) -> Optional[TransferEvent]:
    """Parse a single transfer record from Solscan into a TransferEvent model."""
    sig = (
        item.get("trans_id")
        or item.get("tx_hash")
        or item.get("signature")
        or item.get("txHash")
    )
    if not sig:
        return None

    block_time = item.get("block_time") or item.get("time") or item.get("blockTime")
    if block_time is None:
        return None
    try:
        bt_int = int(block_time)
    except (ValueError, TypeError):
        return None

    from_addr = (
        item.get("from_address")
        or item.get("from")
        or item.get("source")
        or item.get("from_account")
        or ""
    )
    to_addr = (
        item.get("to_address")
        or item.get("to")
        or item.get("destination")
        or item.get("to_account")
        or ""
    )

    amount_val = item.get("amount") or item.get("value") or item.get("balance_change") or 0.0
    try:
        amount_float = float(amount_val)
    except (ValueError, TypeError):
        amount_float = 0.0

    decimals = item.get("decimals") or item.get("token_decimals") or 9
    try:
        decimals_int = int(decimals)
    except (ValueError, TypeError):
        decimals_int = 9

    token_addr = item.get("token_address") or item.get("tokenAddress") or default_token
    activity = item.get("activity_type") or item.get("type") or "transfer"

    return TransferEvent(
        signature=sig,
        block_time=bt_int,
        from_address=from_addr,
        to_address=to_addr,
        token_address=token_addr,
        amount=max(0.0, amount_float),
        decimals=decimals_int,
        activity_type=activity,
    )


def collect_historical_transfers(
    mint_address: str,
    max_transfers: int = 200,
    page_size: int = 50,
    client: Optional[SolscanClient] = None,
    db: Optional[Database] = None,
) -> List[TransferEvent]:
    """Collect historical token transfers chronologically via pagination.
    
    Args:
        mint_address: Solana mint address.
        max_transfers: Maximum number of transfers to gather.
        page_size: Transfers per page request.
        client: Optional SolscanClient instance.
        db: Optional Database instance.
        
    Returns:
        List of TransferEvent models sorted chronologically (ascending).
    """
    valid_mint = validate_solana_address(mint_address)
    active_db = db or Database(settings.sqlite_db_path)
    active_client = client or SolscanClient(database=active_db)

    collected_transfers: List[TransferEvent] = []
    seen_signatures = set()
    page = 1

    while len(collected_transfers) < max_transfers:
        # Respect remaining limit on last page request
        current_page_size = min(page_size, max_transfers - len(collected_transfers))
        if current_page_size <= 0:
            break

        resp = active_client.get_token_transfers(
            token_address=valid_mint,
            page=page,
            page_size=current_page_size,
            sort_by="block_time",
            sort_order="asc",
        )

        items: List[Dict[str, Any]] = []
        if isinstance(resp, dict):
            if "data" in resp:
                data_val = resp["data"]
                if isinstance(data_val, list):
                    items = data_val
                elif isinstance(data_val, dict) and "items" in data_val:
                    items = data_val["items"]
        elif isinstance(resp, list):
            items = resp

        if not items:
            # End of transfer history reached
            break

        new_items_found = 0
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue

            event = _parse_transfer_item(raw_item, valid_mint)
            if not event:
                continue

            # Avoid duplicates if overlapping pages occur
            if event.signature in seen_signatures:
                continue

            seen_signatures.add(event.signature)
            collected_transfers.append(event)
            new_items_found += 1

            # Persist transfer event to database
            active_db.save_wallet_event(
                wallet_address=event.to_address,
                token_address=event.token_address,
                signature=event.signature,
                timestamp=event.block_time,
                event_type="TRANSFER",
                amount=event.amount,
                quote_amount=0.0,
                confidence="LOW",
            )

            if len(collected_transfers) >= max_transfers:
                break

        if new_items_found == 0:
            # No new unique items received on this page
            break

        page += 1

    return collected_transfers
