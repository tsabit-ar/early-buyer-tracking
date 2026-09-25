"""Historical token transfers collector module for EBRS.

Fetches chronological token transfers using Solana Native RPC getSignaturesForAddress
(or Solscan API fallback), parses transfer events, and persists them into SQLite.
"""

import logging
from typing import Any, Dict, List, Optional

from collectors.token import validate_solana_address
from config import settings
from models.schemas import DiscoverySourceEnum, TransferEvent
from storage.database import Database

logger = logging.getLogger(__name__)


class CandidateDiscoveryResult(list):
    """List of TransferEvent models with discovery forensic metadata.
    
    Inherits from list for 100% backward compatibility with all callers
    expecting a List[TransferEvent].
    """
    def __init__(
        self,
        transfers: List[TransferEvent],
        candidate_discovery_complete: bool = True,
        candidate_discovery_truncated: bool = False,
        genesis_reached: bool = True,
        discovery_source: str = DiscoverySourceEnum.UNKNOWN.value,
        discovery_pages_fetched: int = 0,
        discovery_signatures_fetched: int = 0,
        oldest_discovered_block_time: Optional[int] = None,
        oldest_discovered_slot: Optional[int] = None,
        discovery_termination_reason: Optional[str] = None,
        discovery_mode: str = "PRODUCTION",
    ):
        super().__init__(transfers)
        self.candidate_discovery_complete = candidate_discovery_complete
        self.candidate_discovery_truncated = candidate_discovery_truncated
        self.genesis_reached = genesis_reached
        self.discovery_source = discovery_source
        self.discovery_pages_fetched = discovery_pages_fetched
        self.discovery_signatures_fetched = discovery_signatures_fetched
        self.oldest_discovered_block_time = oldest_discovered_block_time
        self.oldest_discovered_slot = oldest_discovered_slot
        self.discovery_termination_reason = discovery_termination_reason
        self.discovery_mode = discovery_mode


def _parse_transfer_item(item: Dict[str, Any], default_token: str) -> Optional[TransferEvent]:
    """Parse a single transfer record from Solscan format into a TransferEvent model."""
    sig = (
        item.get("trans_id")
        or item.get("tx_hash")
        or item.get("signature")
        or item.get("txHash")
    )
    if not sig:
        return None
    if isinstance(sig, list):
        sig = sig[0] if sig else ""
    sig = str(sig)
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
    if isinstance(from_addr, list):
        from_addr = from_addr[0] if from_addr else ""
    from_addr = str(from_addr)

    to_addr = (
        item.get("to_address")
        or item.get("to")
        or item.get("destination")
        or item.get("to_account")
        or ""
    )
    if isinstance(to_addr, list):
        to_addr = to_addr[0] if to_addr else ""
    to_addr = str(to_addr)

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
    if isinstance(token_addr, list):
        token_addr = token_addr[0] if token_addr else default_token
    token_addr = str(token_addr)

    activity = item.get("activity_type") or item.get("type") or "transfer"
    if isinstance(activity, list):
        activity = activity[0] if activity else "transfer"
    activity = str(activity)

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
    client: Optional[Any] = None,
    db: Optional[Database] = None,
    max_pages: int = 10,
    early_window_hours: float = 24.0,
    launch_time: Optional[int] = None,
    discovery_mode: Optional[str] = None,
) -> CandidateDiscoveryResult:
    """Collect historical token transfers chronologically via Solscan ascending or Solana Native RPC fallback.
    
    Priority:
    - If discovery_mode == "FREE_LIMITED":
      1. Primary: Solscan Playground (/playground/token/transfer) within local safety budgets.
         Verifies ascending order. Natural termination + genesis -> complete=True; otherwise incomplete=True.
      2. Fallback: Native Solana RPC getSignaturesForAddress with bounded pagination.
    - If discovery_mode == "PRODUCTION":
      1. Primary: Solscan token transfer API (/v2.0/token/transfer, sort_order="asc", sort_by="block_time").
      2. Fallback: Native Solana RPC getSignaturesForAddress with bounded pagination.

    Args:
        mint_address: Solana mint address.
        max_transfers: Maximum number of transfers to gather.
        page_size: Transfers per page request.
        client: Optional SolanaRpcClient or SolscanClient instance.
        db: Optional Database instance.
        max_pages: Maximum signature pages to traverse backwards if using Native RPC (default: 10).
        early_window_hours: Early buyer discovery window horizon in hours (default: 24.0).
        launch_time: Optional token launch timestamp.
        discovery_mode: Optional runtime mode ("PRODUCTION" or "FREE_LIMITED", defaults to settings.discovery_mode).
        
    Returns:
        CandidateDiscoveryResult (list of TransferEvent models with discovery metadata).
    """
    valid_mint = validate_solana_address(mint_address)
    active_db = db or Database(settings.sqlite_db_path)
    active_mode = discovery_mode or getattr(settings, "discovery_mode", "PRODUCTION")
    
    if client is None:
        from api.solana_rpc import SolanaRpcClient
        active_client = SolanaRpcClient(database=active_db)
    else:
        active_client = client

    # =========================================================================
    # 1. PRIMARY SOURCE SELECTION (FREE_LIMITED vs PRODUCTION)
    # =========================================================================
    if active_mode == "FREE_LIMITED":
        solscan_provider = None
        if hasattr(active_client, "get_playground_token_transfers"):
            solscan_provider = active_client
        elif getattr(settings, "solscan_api_key", None):
            try:
                from api.solscan import SolscanClient
                solscan_provider = SolscanClient(database=active_db)
            except Exception:
                solscan_provider = None
        elif hasattr(active_client, "get_token_transfers"):
            # Mock support in test environments
            solscan_provider = active_client

        if solscan_provider is not None:
            try:
                playground_transfers: List[TransferEvent] = []
                seen_signatures = set()
                page = 1
                pages_fetched = 0
                max_requests = getattr(settings, "free_playground_max_requests_per_run", 3)
                # Solscan Playground strictly requires page_size to be one of [10, 20, 30, 40, 60, 100]
                valid_page_sizes = [10, 20, 30, 40, 60, 100]
                configured_ps = getattr(settings, "free_playground_page_size", 10)
                target_page_size = 10
                for v in valid_page_sizes:
                    if configured_ps <= v:
                        target_page_size = v
                        break
                stream_naturally_exhausted = False

                while len(playground_transfers) < max_transfers and pages_fetched < max_requests:
                    req_page_size = target_page_size

                    if hasattr(solscan_provider, "get_playground_token_transfers"):
                        resp = solscan_provider.get_playground_token_transfers(
                            token_address=valid_mint,
                            page=page,
                            page_size=req_page_size,
                            sort_by="block_time",
                            sort_order="asc",
                        )
                    elif hasattr(solscan_provider, "get_token_transfers"):
                        resp = solscan_provider.get_token_transfers(
                            token_address=valid_mint,
                            page=page,
                            page_size=req_page_size,
                            sort_by="block_time",
                            sort_order="asc",
                        )
                    else:
                        break

                    pages_fetched += 1
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
                        stream_naturally_exhausted = True
                        break

                    # Verify ascending ordering from the response timestamps
                    ts_vals = [
                        item.get("block_time") or item.get("time") or item.get("blockTime")
                        for item in items if isinstance(item, dict)
                    ]
                    valid_ts = [t for t in ts_vals if t is not None]
                    if len(valid_ts) >= 2 and valid_ts[0] > valid_ts[-1]:
                        logger.info("Playground response was descending; enforcing ascending sort by block_time.")
                        items.sort(key=lambda x: (x.get("block_time") or x.get("time") or x.get("blockTime") or 0))

                    new_items_found = 0
                    for raw_item in items:
                        if not isinstance(raw_item, dict):
                            continue

                        event = _parse_transfer_item(raw_item, valid_mint)
                        if not event or event.signature in seen_signatures:
                            continue

                        seen_signatures.add(event.signature)
                        playground_transfers.append(event)
                        new_items_found += 1

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

                        if len(playground_transfers) >= max_transfers:
                            break

                    if new_items_found == 0 or len(items) < req_page_size:
                        stream_naturally_exhausted = True
                        break

                    page += 1

                if playground_transfers:
                    oldest_bt = playground_transfers[0].block_time
                    first_activity = getattr(playground_transfers[0], "activity_type", "")
                    is_mint_genesis = first_activity in ("ACTIVITY_SPL_MINT", "ACTIVITY_TOKEN_MINT", "SPL_MINT")
                    reached_genesis = False
                    if is_mint_genesis:
                        reached_genesis = True
                    elif launch_time is not None and oldest_bt <= launch_time:
                        reached_genesis = True

                    if stream_naturally_exhausted and reached_genesis:
                        cand_complete = True
                        cand_truncated = False
                        gen_reached = True
                        term_reason = "PLAYGROUND_NATURAL_GENESIS"
                    else:
                        cand_complete = False
                        cand_truncated = True
                        gen_reached = reached_genesis
                        if pages_fetched >= max_requests:
                            term_reason = "PLAYGROUND_LOCAL_BUDGET_REACHED"
                        elif len(playground_transfers) >= max_transfers:
                            term_reason = "MAX_TRANSFERS_REACHED"
                        else:
                            term_reason = "PLAYGROUND_PARTIAL_HISTORY"

                    logger.info(
                        f"Candidate discovery completed via Solscan Playground: {len(playground_transfers)} transfers, "
                        f"complete={cand_complete}, reason={term_reason}"
                    )
                    return CandidateDiscoveryResult(
                        transfers=playground_transfers,
                        candidate_discovery_complete=cand_complete,
                        candidate_discovery_truncated=cand_truncated,
                        genesis_reached=gen_reached,
                        discovery_source=DiscoverySourceEnum.SOLSCAN_PLAYGROUND.value,
                        discovery_pages_fetched=pages_fetched,
                        discovery_signatures_fetched=len(playground_transfers),
                        oldest_discovered_block_time=oldest_bt,
                        oldest_discovered_slot=None,
                        discovery_termination_reason=term_reason,
                        discovery_mode="FREE_LIMITED",
                    )
            except Exception as exc:
                logger.info(f"Solscan Playground candidate discovery failed or rate limited ({exc}). Falling back to Native RPC.")

    else:
        # PRODUCTION MODE: Primary Solscan Ascending (/v2.0/token/transfer)
        solscan_provider = None
        if hasattr(active_client, "get_token_transfers"):
            solscan_provider = active_client
        elif getattr(settings, "solscan_api_key", None):
            try:
                from api.solscan import SolscanClient
                solscan_provider = SolscanClient(database=active_db)
            except Exception:
                solscan_provider = None

        if solscan_provider is not None:
            try:
                solscan_transfers: List[TransferEvent] = []
                seen_signatures = set()
                page = 1
                pages_fetched = 0

                while len(solscan_transfers) < max_transfers:
                    current_page_size = min(page_size, max_transfers - len(solscan_transfers))
                    if current_page_size <= 0:
                        break

                    resp = solscan_provider.get_token_transfers(
                        token_address=valid_mint,
                        page=page,
                        page_size=current_page_size,
                        sort_by="block_time",
                        sort_order="asc",
                    )
                    pages_fetched += 1

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
                        break

                    # Verify ascending ordering from the response timestamps
                    ts_vals = [
                        item.get("block_time") or item.get("time") or item.get("blockTime")
                        for item in items if isinstance(item, dict)
                    ]
                    valid_ts = [t for t in ts_vals if t is not None]
                    if len(valid_ts) >= 2 and valid_ts[0] > valid_ts[-1]:
                        logger.info("Solscan response was descending; enforcing ascending sort by block_time.")
                        items.sort(key=lambda x: (x.get("block_time") or x.get("time") or x.get("blockTime") or 0))

                    new_items_found = 0
                    for raw_item in items:
                        if not isinstance(raw_item, dict):
                            continue

                        event = _parse_transfer_item(raw_item, valid_mint)
                        if not event or event.signature in seen_signatures:
                            continue

                        seen_signatures.add(event.signature)
                        solscan_transfers.append(event)
                        new_items_found += 1

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

                        if len(solscan_transfers) >= max_transfers:
                            break

                    if new_items_found == 0:
                        break

                    page += 1

                if solscan_transfers:
                    oldest_bt = solscan_transfers[0].block_time
                    logger.info(
                        f"Candidate discovery completed via Solscan ascending: {len(solscan_transfers)} transfers, "
                        f"oldest_block_time={oldest_bt}"
                    )
                    return CandidateDiscoveryResult(
                        transfers=solscan_transfers,
                        candidate_discovery_complete=True,
                        candidate_discovery_truncated=False,
                        genesis_reached=True,
                        discovery_source=DiscoverySourceEnum.SOLSCAN_ASC.value,
                        discovery_pages_fetched=pages_fetched,
                        discovery_signatures_fetched=len(solscan_transfers),
                        oldest_discovered_block_time=oldest_bt,
                        oldest_discovered_slot=None,
                        discovery_termination_reason="SOLSCAN_ASC_SUCCESS",
                        discovery_mode="PRODUCTION",
                    )
            except Exception as exc:
                logger.info(f"Solscan ascending candidate discovery unavailable or failed ({exc}). Falling back to Native RPC.")

    # =========================================================================
    # 2. FALLBACK SOURCE: Native Solana RPC getSignaturesForAddress (Bounded)
    # =========================================================================
    if hasattr(active_client, "get_signatures_for_address"):
        batches: List[List[Dict[str, Any]]] = []
        before = None
        pages_fetched = 0
        signatures_fetched = 0
        reached_genesis = False
        termination_reason = ""

        while True:
            if len(batches) >= max_pages:
                termination_reason = "MAX_PAGES_REACHED"
                logger.warning(
                    f"collect_historical_transfers reached max_pages limit {max_pages} ({signatures_fetched} sigs) for {valid_mint}."
                )
                break

            try:
                batch = active_client.get_signatures_for_address(valid_mint, limit=1000, before=before)
            except Exception as exc:
                logger.warning(f"RPC error during signature pagination for {valid_mint}: {exc}")
                termination_reason = f"RPC_ERROR: {exc}"
                break

            pages_fetched += 1
            if not batch:
                if signatures_fetched > 0:
                    reached_genesis = True
                    termination_reason = "NATURAL_TERMINATION_EMPTY_BATCH"
                else:
                    termination_reason = "NO_SIGNATURES_FOUND"
                break

            batches.append(batch)
            signatures_fetched += len(batch)
            new_before = batch[-1].get("signature")

            # Stagnant cursor detection
            if new_before == before:
                logger.warning(f"Stagnant cursor detected at signature '{before}' for {valid_mint}.")
                termination_reason = "STAGNANT_CURSOR"
                break

            # Natural termination check (batch size < requested limit 1000)
            if len(batch) < 1000:
                reached_genesis = True
                termination_reason = "NATURAL_TERMINATION"
                logger.info(
                    f"Natural genesis reached for {valid_mint}: batch size {len(batch)} < 1000."
                )
                break

            before = new_before

        collected_transfers: List[TransferEvent] = []
        seen_signatures = set()
        oldest_slot = None

        if batches:
            for batch in reversed(batches):
                for sig_info in reversed(batch):
                    if len(collected_transfers) >= max_transfers:
                        break

                    sig = sig_info.get("signature")
                    if not sig or sig in seen_signatures:
                        continue
                    seen_signatures.add(sig)

                    # Skip failed transactions if err is present
                    if sig_info.get("err"):
                        continue

                    bt = sig_info.get("blockTime") or 0
                    slot = sig_info.get("slot")

                    try:
                        tx_data = active_client.get_transaction_detail(sig)
                    except Exception as exc:
                        logger.debug(f"Failed fetching tx detail for {sig}: {exc}")
                        continue

                    data_dict = tx_data.get("data", {})
                    token_changes = data_dict.get("token_bal_change", [])

                    relevant_changes = [
                        tb for tb in token_changes
                        if tb.get("token_address") == valid_mint
                    ]
                    if not relevant_changes:
                        continue

                    to_addr = ""
                    from_addr = data_dict.get("signer", [""])[0] if data_dict.get("signer") else ""
                    amount = 0.0
                    decs = 9

                    for tb in relevant_changes:
                        chg = tb.get("change", 0.0)
                        if chg > 0:
                            to_addr = tb.get("address", "")
                            amount = chg
                            decs = tb.get("decimals", 9)
                        elif chg < 0:
                            from_addr = tb.get("address", from_addr)

                    if to_addr and amount > 0:
                        if oldest_slot is None and slot:
                            oldest_slot = slot
                        event = TransferEvent(
                            signature=sig,
                            block_time=bt,
                            from_address=from_addr,
                            to_address=to_addr,
                            token_address=valid_mint,
                            amount=amount,
                            decimals=decs,
                            activity_type="transfer",
                        )
                        collected_transfers.append(event)

                        active_db.save_wallet_event(
                            wallet_address=to_addr,
                            token_address=valid_mint,
                            signature=sig,
                            timestamp=bt,
                            event_type="TRANSFER",
                            amount=amount,
                            quote_amount=0.0,
                            confidence="LOW",
                        )

                if len(collected_transfers) >= max_transfers:
                    break

        oldest_bt = collected_transfers[0].block_time if collected_transfers else None
        source_enum = (
            DiscoverySourceEnum.NATIVE_RPC_GENESIS.value
            if reached_genesis
            else DiscoverySourceEnum.NATIVE_RPC_BOUNDED.value
        )
        return CandidateDiscoveryResult(
            transfers=collected_transfers,
            candidate_discovery_complete=reached_genesis,
            candidate_discovery_truncated=not reached_genesis,
            genesis_reached=reached_genesis,
            discovery_source=source_enum,
            discovery_pages_fetched=pages_fetched,
            discovery_signatures_fetched=signatures_fetched,
            oldest_discovered_block_time=oldest_bt,
            oldest_discovered_slot=oldest_slot,
            discovery_termination_reason=termination_reason or ("NATURAL_TERMINATION" if reached_genesis else "INCOMPLETE"),
            discovery_mode=active_mode,
        )

    # Empty fallback if no client methods available
    return CandidateDiscoveryResult(
        transfers=[],
        candidate_discovery_complete=False,
        candidate_discovery_truncated=True,
        genesis_reached=False,
        discovery_source=DiscoverySourceEnum.UNKNOWN.value,
        discovery_pages_fetched=0,
        discovery_signatures_fetched=0,
        oldest_discovered_block_time=None,
        oldest_discovered_slot=None,
        discovery_termination_reason="NO_USABLE_CLIENT",
        discovery_mode=active_mode,
    )
