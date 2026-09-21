"""Token collector module for Early Buyer Scanner (EBRS).

Validates Solana mint addresses, fetches token metadata via Solscan Pro API,
and determines token launch timestamp and confidence.
"""

import re
from typing import Any, Dict, Optional, Tuple

from api.solscan import SolscanClient
from config import settings
from models.schemas import ConfidenceEnum, TokenMetadata
from storage.database import Database

# Standard Bitcoin/Solana Base58 alphabet (no 0, O, I, l)
BASE58_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def validate_solana_address(address: str) -> str:
    """Validate that an address conforms to Solana's Base58 32-44 character format.
    
    Raises:
        ValueError: If address is empty, incorrect length, or contains non-Base58 characters.
    """
    if not address or not isinstance(address, str):
        raise ValueError("Solana address must be a non-empty string.")
    
    trimmed = address.strip()
    if not (32 <= len(trimmed) <= 44):
        raise ValueError(
            f"Invalid Solana address length ({len(trimmed)}). Expected between 32 and 44 characters: '{trimmed}'"
        )
    
    if not BASE58_PATTERN.match(trimmed):
        raise ValueError(
            f"Invalid Base58 characters in Solana address: '{trimmed}'. "
            "Characters '0', 'O', 'I', and 'l' are forbidden in Base58."
        )
    
    return trimmed


def fetch_token_metadata(
    mint_address: str,
    client: Optional[SolscanClient] = None,
    db: Optional[Database] = None,
) -> TokenMetadata:
    """Retrieve token metadata from Solscan API and persist to SQLite tokens table.
    
    Args:
        mint_address: The Solana mint address.
        client: Optional SolscanClient instance.
        db: Optional Database instance.
        
    Returns:
        TokenMetadata Pydantic model.
    """
    valid_mint = validate_solana_address(mint_address)
    active_db = db or Database(settings.sqlite_db_path)
    active_client = client or SolscanClient(database=active_db)

    raw_response = active_client.get_token_meta(valid_mint)

    # Handle various response wrapper shapes from Solscan API
    data: Dict[str, Any] = {}
    if isinstance(raw_response, dict):
        if "data" in raw_response and isinstance(raw_response["data"], dict):
            data = raw_response["data"]
        else:
            data = raw_response

    # Extract metadata fields with safe fallbacks
    token_address = data.get("address") or data.get("token_address") or valid_mint
    name = data.get("name")
    symbol = data.get("symbol")
    decimals = data.get("decimals")
    try:
        decimals_int = int(decimals) if decimals is not None else 9
    except (ValueError, TypeError):
        decimals_int = 9

    creator = data.get("creator") or data.get("owner") or data.get("authority")

    # Check if we already have launch_time and launch_confidence stored
    existing = active_db.get_token(valid_mint)
    launch_time = existing.launch_time if existing else None
    launch_confidence = existing.launch_confidence if existing else ConfidenceEnum.LOW

    metadata = TokenMetadata(
        token_address=token_address,
        name=name,
        symbol=symbol,
        decimals=decimals_int,
        creator=creator,
        launch_time=launch_time,
        launch_confidence=launch_confidence,
    )

    active_db.save_token(metadata)
    return metadata


def resolve_launch_time(
    mint_address: str,
    client: Optional[SolscanClient] = None,
    db: Optional[Database] = None,
) -> Tuple[Optional[int], str]:
    """Resolve token launch timestamp by querying the earliest historical transfer.
    
    Args:
        mint_address: The Solana mint address.
        client: Optional SolscanClient instance.
        db: Optional Database instance.
        
    Returns:
        Tuple of (launch_time, launch_confidence).
    """
    valid_mint = validate_solana_address(mint_address)
    active_db = db or Database(settings.sqlite_db_path)
    active_client = client or SolscanClient(database=active_db)

    # Query earliest transfer
    resp = active_client.get_token_transfers(
        token_address=valid_mint,
        page=1,
        page_size=1,
        sort_by="block_time",
        sort_order="asc",
    )

    items = []
    if isinstance(resp, dict):
        if "data" in resp:
            data_val = resp["data"]
            if isinstance(data_val, list):
                items = data_val
            elif isinstance(data_val, dict) and "items" in data_val:
                items = data_val["items"]
    elif isinstance(resp, list):
        items = resp

    launch_time: Optional[int] = None
    launch_confidence: str = ConfidenceEnum.LOW.value

    if items and len(items) > 0:
        first_tx = items[0]
        bt = (
            first_tx.get("block_time")
            or first_tx.get("time")
            or first_tx.get("blockTime")
        )
        if bt is not None:
            try:
                launch_time = int(bt)
                launch_confidence = ConfidenceEnum.HIGH.value
            except (ValueError, TypeError):
                launch_time = None
                launch_confidence = ConfidenceEnum.LOW.value

    # Update or persist token with newly resolved launch time
    token_record = active_db.get_token(valid_mint)
    if token_record:
        token_record.launch_time = launch_time
        token_record.launch_confidence = ConfidenceEnum(launch_confidence)
        active_db.save_token(token_record)
    else:
        # Create minimal record if metadata hasn't been fetched yet
        new_token = TokenMetadata(
            token_address=valid_mint,
            launch_time=launch_time,
            launch_confidence=ConfidenceEnum(launch_confidence),
        )
        active_db.save_token(new_token)

    return launch_time, launch_confidence
