"""Token collector module for Early Buyer Scanner (EBRS).

Validates Solana mint addresses, resolves token metadata via Metaplex Metadata PDA,
Token-2022 extensions, or RPC fallback, and determines launch timestamp.
"""

import base64
import logging
import re
import struct
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from api.solscan import SolscanClient
from config import settings
from models.schemas import (
    ConfidenceEnum,
    LaunchResolutionType,
    LaunchTimeResolution,
    TokenMetadata,
)
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
    launch_time_type = (
        existing.launch_time_type
        if existing and hasattr(existing, "launch_time_type") and existing.launch_time_type
        else LaunchResolutionType.UNKNOWN.value
    )

    metadata = TokenMetadata(
        token_address=valid_mint,
        name=name,
        symbol=symbol,
        decimals=decimals_int,
        creator=creator,
        launch_time=launch_time,
        launch_confidence=launch_confidence,
        launch_time_type=launch_time_type,
    )

    active_db.save_token(metadata)
    return metadata


def fetch_dexscreener_pair_created_at(
    mint_address: str, timeout: float = 5.0
) -> Optional[Tuple[int, str, str]]:
    """Fetch earliest pair creation timestamp from DexScreener for a mint.

    Args:
        mint_address: Solana token mint address.
        timeout: HTTP request timeout in seconds.

    Returns:
        Tuple of (pair_created_at_seconds, dex_id, pair_address) or None.
    """
    try:
        resp = httpx.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}",
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs") or []
            if isinstance(pairs, list) and pairs:
                valid_pairs = []
                for p in pairs:
                    pca = p.get("pairCreatedAt")
                    if pca and isinstance(pca, (int, float)) and pca > 0:
                        valid_pairs.append((
                            int(pca // 1000),
                            str(p.get("dexId", "unknown")),
                            str(p.get("pairAddress", "")),
                        ))
                if valid_pairs:
                    # Sort by created_at ascending to find the earliest pool
                    valid_pairs.sort(key=lambda x: x[0])
                    return valid_pairs[0]
    except Exception as exc:
        logger.debug(f"DexScreener pairCreatedAt query error for {mint_address}: {exc}")
    return None


def resolve_launch_time(
    mint_address: str,
    client: Optional[Any] = None,
    db: Optional[Database] = None,
    max_pages: int = 5,
    max_signatures: int = 5000,
    max_elapsed_seconds: float = 10.0,
    force_refresh: bool = False,
) -> LaunchTimeResolution:
    """Resolve token launch timestamp with bounded pagination and evidence-first classification.
    
    Guarantees strict termination:
    - Never loops unbounded on high-volume tokens.
    - Limits RPC traversal by max_pages, max_signatures, and max_elapsed_seconds.
    - Detects stagnant cursor to prevent infinite loops.
    
    Evidence hierarchy:
    1. EXACT GENESIS (HIGH confidence): On-chain ledger history naturally terminated
       (batch size < limit), reaching the InitializeMint creation transaction.
    2. ESTIMATED LAUNCH (MEDIUM confidence): Bounded limit hit; resolved via DEX liquidity
       pool creation timestamp (e.g. DexScreener pairCreatedAt).
    3. BOUNDED OLDEST (LOW confidence): Bounded limit hit and DEX pool unavailable;
       using oldest signature seen in bounded window.
    4. UNKNOWN: No signatures or pool data available.

    Args:
        mint_address: Solana mint address.
        client: Optional SolanaRpcClient or SolscanClient instance.
        db: Optional Database instance.
        max_pages: Maximum RPC signature pages to fetch (default: 5).
        max_signatures: Maximum total signatures to fetch (default: 5000).
        max_elapsed_seconds: Maximum wall-clock seconds for RPC queries (default: 10.0).
        force_refresh: If True, bypass SQLite cache and re-query.
        
    Returns:
        LaunchTimeResolution instance (can be unpacked as (launch_time, launch_confidence)).
    """
    valid_mint = validate_solana_address(mint_address)
    active_db = db or Database(settings.sqlite_db_path)
    
    # 1. Check existing DB record for valid launch_time (SQLite Idempotency)
    if not force_refresh:
        existing = active_db.get_token(valid_mint)
        if existing and existing.launch_time and existing.launch_time > 0:
            conf_enum = (
                existing.launch_confidence
                if isinstance(existing.launch_confidence, ConfidenceEnum)
                else ConfidenceEnum(existing.launch_confidence)
            )
            conf_val = conf_enum.value
            cached_type = (
                existing.launch_time_type
                if getattr(existing, "launch_time_type", None) and existing.launch_time_type != LaunchResolutionType.UNKNOWN.value
                else LaunchResolutionType.CACHED_DB.value
            )
            logger.info(f"Using cached launch time for {valid_mint}: {existing.launch_time} ({conf_val}, type={cached_type})")
            return LaunchTimeResolution(
                token_address=valid_mint,
                launch_time=existing.launch_time,
                confidence=conf_enum,
                resolution_type=cached_type,
                evidence_details="Retrieved from SQLite database cache",
                termination_reason="CACHE_HIT",
            )

    if client is None:
        from api.solana_rpc import SolanaRpcClient
        active_client = SolanaRpcClient(database=active_db)
    else:
        active_client = client

    start_time = time.time()
    pages_fetched = 0
    signatures_fetched = 0
    termination_reason = ""
    reached_natural_genesis = False
    oldest_sig_info: Optional[Dict[str, Any]] = None

    # Branch 1: Native Solana RPC client with get_signatures_for_address
    if hasattr(active_client, "get_signatures_for_address"):
        try:
            before = None
            while True:
                # 1. Timeout check
                elapsed = time.time() - start_time
                if elapsed >= max_elapsed_seconds:
                    termination_reason = "TIMEOUT_REACHED"
                    logger.warning(
                        f"resolve_launch_time for {valid_mint} hit timeout {max_elapsed_seconds}s "
                        f"after {pages_fetched} pages ({signatures_fetched} sigs)."
                    )
                    break

                # 2. Max pages check
                if pages_fetched >= max_pages:
                    termination_reason = "MAX_PAGES_REACHED"
                    logger.info(
                        f"resolve_launch_time for {valid_mint} hit max pages {max_pages} "
                        f"({signatures_fetched} sigs)."
                    )
                    break

                # 3. Max signatures check
                remaining_sigs = max_signatures - signatures_fetched
                if remaining_sigs <= 0:
                    termination_reason = "MAX_SIGNATURES_REACHED"
                    break

                page_limit = min(1000, remaining_sigs)
                batch = active_client.get_signatures_for_address(valid_mint, limit=page_limit, before=before)
                pages_fetched += 1

                if not batch:
                    if signatures_fetched > 0:
                        reached_natural_genesis = True
                        termination_reason = "NATURAL_TERMINATION_EMPTY_BATCH"
                    else:
                        termination_reason = "NO_SIGNATURES_FOUND"
                    break

                signatures_fetched += len(batch)
                last_sig = batch[-1]
                new_before = last_sig.get("signature")

                # Stagnant cursor detection
                if new_before == before:
                    logger.warning(f"Stagnant cursor detected at signature '{before}' for {valid_mint}.")
                    termination_reason = "STAGNANT_CURSOR"
                    oldest_sig_info = last_sig
                    break

                oldest_sig_info = last_sig
                before = new_before

                # Natural termination check: batch returned fewer than requested limit
                if len(batch) < page_limit:
                    reached_natural_genesis = True
                    termination_reason = "NATURAL_TERMINATION"
                    logger.info(
                        f"Reached natural genesis for {valid_mint}: batch size {len(batch)} < {page_limit}. "
                        f"Genesis signature: {oldest_sig_info.get('signature')}"
                    )
                    break

        except Exception as exc:
            logger.warning(f"Error resolving launch time via Solana RPC: {exc}")
            termination_reason = f"RPC_ERROR: {exc}"

    # Branch 2: Solscan fallback client (if client does not have get_signatures_for_address)
    elif hasattr(active_client, "get_token_transfers"):
        try:
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
                        bt_int = int(bt)
                        reached_natural_genesis = True
                        oldest_sig_info = {
                            "blockTime": bt_int,
                            "signature": first_tx.get("tx_hash") or first_tx.get("trans_id") or first_tx.get("signature"),
                            "slot": first_tx.get("slot"),
                        }
                        termination_reason = "SOLSCAN_ASC_FIRST_TX"
                    except (ValueError, TypeError):
                        pass
        except Exception as exc:
            logger.warning(f"Error resolving launch time via Solscan: {exc}")
            termination_reason = f"SOLSCAN_ERROR: {exc}"

    # Evaluate Resolution & Confidence
    launch_time: Optional[int] = None
    confidence: ConfidenceEnum = ConfidenceEnum.UNKNOWN
    resolution_type: str = LaunchResolutionType.UNKNOWN.value
    evidence_sig: Optional[str] = None
    evidence_slot: Optional[int] = None
    evidence_details: Optional[str] = None

    if reached_natural_genesis and oldest_sig_info and oldest_sig_info.get("blockTime") is not None:
        # EXACT GENESIS FOUND
        launch_time = int(oldest_sig_info["blockTime"])
        confidence = ConfidenceEnum.HIGH
        resolution_type = LaunchResolutionType.EXACT_GENESIS.value
        evidence_sig = oldest_sig_info.get("signature")
        evidence_slot = oldest_sig_info.get("slot")
        evidence_details = (
            f"On-chain genesis reached naturally ({pages_fetched} page(s), {signatures_fetched} sig(s)). "
            f"Signature: {evidence_sig}, Slot: {evidence_slot}"
        )
    else:
        # Bounded limits reached or genesis not naturally reached -> Graded Fallback
        # Fallback 1: DEX pool creation timestamp (DexScreener pairCreatedAt)
        dex_pool = fetch_dexscreener_pair_created_at(valid_mint)
        if dex_pool:
            pool_time, dex_id, pair_addr = dex_pool
            launch_time = pool_time
            confidence = ConfidenceEnum.MEDIUM
            resolution_type = LaunchResolutionType.ESTIMATED_POOL_CREATION.value
            evidence_details = (
                f"Bounded search stopped ({termination_reason}, {signatures_fetched} sigs examined). "
                f"Estimated via first DEX liquidity pool on {dex_id} (pair: {pair_addr}) at {pool_time} UTC"
            )
        elif oldest_sig_info and oldest_sig_info.get("blockTime") is not None:
            # Fallback 2: Oldest signature seen within bounded window
            launch_time = int(oldest_sig_info["blockTime"])
            confidence = ConfidenceEnum.LOW
            resolution_type = LaunchResolutionType.BOUNDED_OLDEST_SIGNATURE.value
            evidence_sig = oldest_sig_info.get("signature")
            evidence_slot = oldest_sig_info.get("slot")
            evidence_details = (
                f"Bounded search stopped ({termination_reason}). "
                f"Oldest signature in bounded window ({signatures_fetched} sigs examined). "
                f"Earlier transactions exist on-chain."
            )
        else:
            # Fallback 3: No valid timestamp found
            launch_time = None
            confidence = ConfidenceEnum.LOW
            resolution_type = LaunchResolutionType.UNKNOWN.value
            evidence_details = f"No signatures or pool data found ({termination_reason})."

    total_elapsed = round(time.time() - start_time, 3)

    # Update or persist token with newly resolved launch time
    token_record = active_db.get_token(valid_mint)
    if token_record:
        token_record.launch_time = launch_time
        token_record.launch_confidence = confidence
        token_record.launch_time_type = resolution_type
        active_db.save_token(token_record)
    else:
        # Create minimal record if metadata hasn't been fetched yet
        new_token = TokenMetadata(
            token_address=valid_mint,
            launch_time=launch_time,
            launch_confidence=confidence,
            launch_time_type=resolution_type,
        )
        active_db.save_token(new_token)

    resolution = LaunchTimeResolution(
        token_address=valid_mint,
        launch_time=launch_time,
        confidence=confidence,
        resolution_type=resolution_type,
        evidence_signature=evidence_sig,
        evidence_slot=evidence_slot,
        evidence_details=evidence_details,
        pages_fetched=pages_fetched,
        signatures_fetched=signatures_fetched,
        elapsed_seconds=total_elapsed,
        termination_reason=termination_reason,
    )

    logger.info(
        f"Launch time resolved for {valid_mint}: time={launch_time}, "
        f"confidence={confidence.value}, type={resolution_type}, "
        f"sigs={signatures_fetched}, elapsed={total_elapsed}s, reason={termination_reason}"
    )

    return resolution
