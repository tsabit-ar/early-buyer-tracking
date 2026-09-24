"""Candidate token lifecycle acquisition module for Early Buyer Scanner (EBRS).

Implements comprehensive lifecycle acquisition via candidate Associated Token Accounts (ATAs):
- Derives deterministic ATAs for both standard SPL Token and Token-2022.
- Paginates getSignaturesForAddress across candidate ATAs to capture 100% of historical transactions (BUY/SELL/TRANSFER/CLOSE).
- Tracks lifecycle completeness metadata and avoids silent truncation.
"""

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Optional, Set

from solders.pubkey import Pubkey

from config import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)

logger = logging.getLogger(__name__)

ATA_PROGRAM_PUBKEY = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
TOKEN_PROGRAM_PUBKEY = Pubkey.from_string(TOKEN_PROGRAM_ID)
TOKEN_2022_PUBKEY = Pubkey.from_string(TOKEN_2022_PROGRAM_ID)


@dataclass
class CandidateLifecycleInfo:
    """Tracking metadata for a candidate's complete on-chain token lifecycle."""
    wallet_address: str
    token_mint: str
    lifecycle_atas: List[str] = field(default_factory=list)
    signatures: List[str] = field(default_factory=list)
    lifecycle_signature_count: int = 0
    lifecycle_pages_fetched: int = 0
    lifecycle_history_complete: bool = True
    lifecycle_truncated: bool = False
    truncation_reason: Optional[str] = None
    last_cursor_signature: Optional[str] = None


def derive_candidate_atas(wallet_address: str, token_mint: str) -> List[str]:
    """Derive Associated Token Account (ATA) addresses for a wallet across both SPL Token and Token-2022.
    
    Args:
        wallet_address: Base58 string of candidate wallet public key.
        token_mint: Base58 string of token mint public key.
        
    Returns:
        List of derived ATA addresses (Base58 strings).
    """
    clean_wallet = wallet_address.strip()
    clean_mint = token_mint.strip()
    if not clean_wallet or not clean_mint:
        return []

    try:
        wallet_pk = Pubkey.from_string(clean_wallet)
        mint_pk = Pubkey.from_string(clean_mint)
    except Exception as exc:
        logger.warning(f"Failed to parse public keys for ATA derivation ({clean_wallet}, {clean_mint}): {exc}")
        return []

    derived: List[str] = []
    
    # 1. Token-2022 ATA (seeds: [owner, TOKEN_2022, mint])
    try:
        ata_2022, _ = Pubkey.find_program_address(
            [bytes(wallet_pk), bytes(TOKEN_2022_PUBKEY), bytes(mint_pk)],
            ATA_PROGRAM_PUBKEY,
        )
        derived.append(str(ata_2022))
    except Exception as exc:
        logger.debug(f"Error deriving Token-2022 ATA: {exc}")

    # 2. Standard SPL Token ATA (seeds: [owner, TOKEN_PROGRAM, mint])
    try:
        ata_spl, _ = Pubkey.find_program_address(
            [bytes(wallet_pk), bytes(TOKEN_PROGRAM_PUBKEY), bytes(mint_pk)],
            ATA_PROGRAM_PUBKEY,
        )
        ata_spl_str = str(ata_spl)
        if ata_spl_str not in derived:
            derived.append(ata_spl_str)
    except Exception as exc:
        logger.debug(f"Error deriving standard SPL ATA: {exc}")

    return derived


def fetch_candidate_lifecycle_signatures(
    client: Any,
    candidate_wallets: List[str],
    token_mint: str,
    max_signatures_per_ata: Optional[int] = 500,
) -> Dict[str, CandidateLifecycleInfo]:
    """Fetch complete historical transaction signatures for candidate wallets via their ATAs.
    
    Paginates getSignaturesForAddress until no further signatures remain or until an explicit
    safety limit is reached. If truncated by safety limit, records lifecycle_history_complete=False.
    
    Args:
        client: SolanaRpcClient instance (or SolscanClient with get_signatures_for_address).
        candidate_wallets: List of candidate wallet addresses.
        token_mint: Token mint address.
        max_signatures_per_ata: Optional safety limit per ATA (default 500). Set to None for unlimited.
        
    Returns:
        Dictionary mapping wallet_address -> CandidateLifecycleInfo.
    """
    results: Dict[str, CandidateLifecycleInfo] = {}

    for wallet in candidate_wallets:
        clean_wallet = wallet.strip()
        if not clean_wallet:
            continue

        atas = derive_candidate_atas(clean_wallet, token_mint)
        info = CandidateLifecycleInfo(
            wallet_address=clean_wallet,
            token_mint=token_mint,
            lifecycle_atas=atas,
        )

        all_wallet_sigs: List[str] = []
        is_complete = True
        is_truncated = False
        trunc_reason: Optional[str] = None
        total_pages_fetched = 0
        last_cursor: Optional[str] = None

        for ata in atas:
            before_sig: Optional[str] = None
            ata_sigs: List[str] = []

            while True:
                batch_limit = 100
                if max_signatures_per_ata is not None:
                    remaining = max_signatures_per_ata - len(ata_sigs)
                    if remaining <= 0:
                        is_complete = False
                        is_truncated = True
                        trunc_reason = f"Hit safety limit of {max_signatures_per_ata} signatures while more history exists on ATA {ata}"
                        logger.warning(f"Candidate {clean_wallet}: {trunc_reason}")
                        break
                    batch_limit = min(100, remaining)

                try:
                    if hasattr(client, "get_signatures_for_address"):
                        resp = client.get_signatures_for_address(
                            ata,
                            limit=batch_limit,
                            before=before_sig,
                        )
                    else:
                        resp = []
                except Exception as exc:
                    logger.warning(f"Error querying signatures for ATA {ata}: {exc}")
                    is_complete = False
                    is_truncated = True
                    trunc_reason = f"RPC error while querying ATA {ata}: {exc}"
                    break

                total_pages_fetched += 1

                if not resp or not isinstance(resp, list):
                    # No signatures returned: ledger history exhausted for this ATA
                    break

                for item in resp:
                    if isinstance(item, dict) and "signature" in item:
                        sig = str(item["signature"]).strip()
                        if sig and sig not in ata_sigs:
                            ata_sigs.append(sig)
                    elif isinstance(item, str):
                        sig = item.strip()
                        if sig and sig not in ata_sigs:
                            ata_sigs.append(sig)

                # If returned less than requested batch_limit, reached oldest transaction on ledger
                if len(resp) < batch_limit:
                    break

                # Prepare next page
                last_sig = resp[-1].get("signature") if isinstance(resp[-1], dict) else resp[-1]
                if last_sig:
                    last_cursor = str(last_sig).strip()
                if last_sig == before_sig or not last_sig:
                    break
                before_sig = str(last_sig).strip()

                # If this full page reached safety limit, history is truncated
                if max_signatures_per_ata is not None and len(ata_sigs) >= max_signatures_per_ata:
                    is_complete = False
                    is_truncated = True
                    trunc_reason = f"Hit safety limit of {max_signatures_per_ata} signatures on full page on ATA {ata}"
                    logger.warning(f"Candidate {clean_wallet}: {trunc_reason}")
                    break

            for s in ata_sigs:
                if s not in all_wallet_sigs:
                    all_wallet_sigs.append(s)

        info.signatures = all_wallet_sigs
        info.lifecycle_signature_count = len(all_wallet_sigs)
        info.lifecycle_pages_fetched = total_pages_fetched
        info.lifecycle_history_complete = is_complete
        info.lifecycle_truncated = is_truncated
        info.truncation_reason = trunc_reason
        info.last_cursor_signature = last_cursor
        results[clean_wallet] = info

    return results
