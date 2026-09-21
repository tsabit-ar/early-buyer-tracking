"""Token collector module for Early Buyer Scanner (EBRS).

Validates Solana mint addresses, resolves token metadata via Metaplex Metadata PDA,
Token-2022 extensions, or RPC fallback, and determines launch timestamp.
"""

import base64
import logging
import re
import struct
from typing import Any, Dict, Optional, Tuple

import httpx

from api.solscan import SolscanClient
from config import settings
from models.schemas import ConfidenceEnum, TokenMetadata
from storage.database import Database

logger = logging.getLogger(__name__)

# Standard Bitcoin/Solana Base58 alphabet (no 0, O, I, l)
BASE58_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
METAPLEX_PROGRAM_ID = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"


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


def derive_metaplex_metadata_pda(mint_address: str) -> str:
    """Derive the Metaplex Token Metadata PDA address for a given token mint.
    
    Seeds: [b"metadata", bytes(PublicKey("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")), bytes(PublicKey(mint_address))]
    Program ID: "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
    
    Returns:
        Base58 encoded PDA address string.
    """
    from solders.pubkey import Pubkey
    metadata_program = Pubkey.from_string(METAPLEX_PROGRAM_ID)
    mint_pubkey = Pubkey.from_string(mint_address)
    seeds = [b"metadata", bytes(metadata_program), bytes(mint_pubkey)]
    pda, _ = Pubkey.find_program_address(seeds, metadata_program)
    return str(pda)


def decode_metaplex_metadata(raw_bytes: bytes) -> Tuple[Optional[str], Optional[str]]:
    """Decode binary Metaplex Metadata account data to extract token name and symbol.
    
    Metaplex Metadata Layout (MetadataV1):
    - Offset 0: Key (1 byte)
    - Offset 1: Update authority (32 bytes)
    - Offset 33: Mint (32 bytes)
    - Offset 65: Name length (4 bytes LE uint32)
    - Offset 69: Name string (padded with null bytes \x00)
    - Offset 69 + name_len: Symbol length (4 bytes LE uint32)
    - Offset 69 + name_len + 4: Symbol string (padded with null bytes \x00)
    
    Returns:
        Tuple of (name, symbol) or (None, None).
    """
    if len(raw_bytes) < 69:
        return None, None

    try:
        offset = 65
        name_len = struct.unpack("<I", raw_bytes[offset:offset + 4])[0]
        offset += 4
        if offset + name_len > len(raw_bytes):
            return None, None
        name_str = raw_bytes[offset:offset + name_len].decode("utf-8", errors="ignore").strip("\x00").strip()
        offset += name_len

        if offset + 4 > len(raw_bytes):
            return (name_str or None), None

        symbol_len = struct.unpack("<I", raw_bytes[offset:offset + 4])[0]
        offset += 4
        if offset + symbol_len > len(raw_bytes):
            return (name_str or None), None
        symbol_str = raw_bytes[offset:offset + symbol_len].decode("utf-8", errors="ignore").strip("\x00").strip()

        return (name_str or None), (symbol_str or None)
    except Exception as exc:
        logger.debug(f"Failed decoding Metaplex metadata payload: {exc}")
        return None, None


def fetch_token_metadata(
    mint_address: str,
    client: Optional[Any] = None,
    db: Optional[Database] = None,
) -> TokenMetadata:
    """Retrieve token metadata from RPC/Metaplex/Solscan and persist to SQLite tokens table.
    
    Args:
        mint_address: The Solana mint address.
        client: Optional SolanaRpcClient or SolscanClient instance.
        db: Optional Database instance.
        
    Returns:
        TokenMetadata Pydantic model.
    """
    valid_mint = validate_solana_address(mint_address)
    active_db = db or Database(settings.sqlite_db_path)
    
    if client is None:
        from api.solana_rpc import SolanaRpcClient
        active_client = SolanaRpcClient(database=active_db)
    else:
        active_client = client

    token_address = valid_mint
    name: Optional[str] = None
    symbol: Optional[str] = None
    decimals_int = 9
    creator: Optional[str] = None

    # Check existing DB record
    existing = active_db.get_token(valid_mint)
    if existing and existing.name and existing.name != "UNKNOWN" and existing.symbol:
        name = existing.name
        symbol = existing.symbol
        decimals_int = existing.decimals
        creator = existing.creator

    # 1. Metaplex Metadata PDA on-chain resolution (Standard SPL Tokens)
    if not name or not symbol:
        try:
            pda_address = derive_metaplex_metadata_pda(valid_mint)
            if hasattr(active_client, "get_account_info"):
                pda_info = active_client.get_account_info(pda_address, encoding="base64")
                if isinstance(pda_info, dict) and "data" in pda_info:
                    raw_data_field = pda_info["data"]
                    raw_b64 = raw_data_field[0] if isinstance(raw_data_field, list) else raw_data_field
                    if isinstance(raw_b64, str):
                        raw_bytes = base64.b64decode(raw_b64)
                        meta_name, meta_symbol = decode_metaplex_metadata(raw_bytes)
                        if meta_name:
                            name = meta_name
                        if meta_symbol:
                            symbol = meta_symbol
        except Exception as exc:
            logger.debug(f"Metaplex PDA query encountered error: {exc}")

    # 2. Token-2022 Extensions / Mint Account resolution (Modern Solana Tokens, e.g. ACAT)
    if hasattr(active_client, "get_account_info"):
        try:
            acc_info = active_client.get_account_info(valid_mint, encoding="jsonParsed")
            if isinstance(acc_info, dict) and "data" in acc_info:
                d = acc_info["data"]
                if isinstance(d, dict) and "parsed" in d:
                    info = d["parsed"].get("info", {})
                    if not creator:
                        creator = info.get("mintAuthority")
                    if "decimals" in info:
                        try:
                            decimals_int = int(info["decimals"])
                        except (ValueError, TypeError):
                            pass

                    # Parse embedded Token-2022 tokenMetadata extension
                    extensions = info.get("extensions", [])
                    for ext in extensions:
                        if isinstance(ext, dict) and ext.get("extension") == "tokenMetadata":
                            state = ext.get("state", {})
                            if not name and state.get("name"):
                                name = str(state["name"]).strip()
                            if not symbol and state.get("symbol"):
                                symbol = str(state["symbol"]).strip()
        except Exception as exc:
            logger.debug(f"Token-2022 extensions query encountered error: {exc}")

    # 3. DexScreener Public API fallback
    if not name or not symbol:
        try:
            dex_resp = httpx.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{valid_mint}",
                timeout=5.0
            )
            if dex_resp.status_code == 200:
                pairs = dex_resp.json().get("pairs", [])
                if pairs and isinstance(pairs, list):
                    base = pairs[0].get("baseToken", {})
                    if not name and base.get("name"):
                        name = str(base["name"]).strip()
                    if not symbol and base.get("symbol"):
                        symbol = str(base["symbol"]).strip()
        except Exception as exc:
            logger.debug(f"DexScreener API fallback error: {exc}")

    # 4. Solscan Pro fallback (if available)
    if (not name or not symbol) and hasattr(active_client, "get_token_meta"):
        try:
            raw_response = active_client.get_token_meta(valid_mint)
            data: Dict[str, Any] = {}
            if isinstance(raw_response, dict):
                if "data" in raw_response and isinstance(raw_response["data"], dict):
                    data = raw_response["data"]
                else:
                    data = raw_response
            if not name:
                name = data.get("name")
            if not symbol:
                symbol = data.get("symbol")
            if not creator:
                creator = data.get("creator") or data.get("owner") or data.get("authority")
            if "decimals" in data and data["decimals"] is not None:
                try:
                    decimals_int = int(data["decimals"])
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    # 5. Token supply decimals check
    if hasattr(active_client, "get_token_supply"):
        try:
            supply_data = active_client.get_token_supply(valid_mint)
            if isinstance(supply_data, dict) and "decimals" in supply_data:
                decimals_int = int(supply_data["decimals"])
        except Exception:
            pass

    # Fallback to defaults
    if not name and symbol:
        name = symbol
    elif not symbol and name:
        symbol = name

    launch_time = existing.launch_time if existing else None
    launch_confidence = existing.launch_confidence if existing else ConfidenceEnum.LOW

    metadata = TokenMetadata(
        token_address=valid_mint,
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
    client: Optional[Any] = None,
    db: Optional[Database] = None,
) -> Tuple[Optional[int], str]:
    """Resolve token launch timestamp by querying the earliest historical transfer/signature.
    
    Args:
        mint_address: The Solana mint address.
        client: Optional SolanaRpcClient or SolscanClient instance.
        db: Optional Database instance.
        
    Returns:
        Tuple of (launch_time, launch_confidence).
    """
    valid_mint = validate_solana_address(mint_address)
    active_db = db or Database(settings.sqlite_db_path)
    
    # 1. Check existing DB record for valid launch_time (SQLite Idempotency)
    existing = active_db.get_token(valid_mint)
    if existing and existing.launch_time and existing.launch_time > 0:
        conf_val = (
            existing.launch_confidence.value
            if hasattr(existing.launch_confidence, "value")
            else str(existing.launch_confidence)
        )
        logger.info(f"Using cached launch time for {valid_mint}: {existing.launch_time} ({conf_val})")
        return existing.launch_time, conf_val

    if client is None:
        from api.solana_rpc import SolanaRpcClient
        active_client = SolanaRpcClient(database=active_db)
    else:
        active_client = client

    launch_time: Optional[int] = None
    launch_confidence: str = ConfidenceEnum.LOW.value

    # If client has native Solana RPC get_signatures_for_address
    if hasattr(active_client, "get_signatures_for_address"):
        try:
            before = None
            oldest_sig_info = None
            while True:
                batch = active_client.get_signatures_for_address(valid_mint, limit=1000, before=before)
                if not batch:
                    break
                oldest_sig_info = batch[-1]
                if len(batch) < 1000:
                    break
                before = oldest_sig_info.get("signature")

            if oldest_sig_info and oldest_sig_info.get("blockTime") is not None:
                launch_time = int(oldest_sig_info["blockTime"])
                launch_confidence = ConfidenceEnum.HIGH.value
        except Exception as exc:
            logger.warning(f"Error resolving launch time via Solana RPC: {exc}")

    # If client has get_token_transfers (Solscan fallback)
    elif hasattr(active_client, "get_token_transfers"):
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
