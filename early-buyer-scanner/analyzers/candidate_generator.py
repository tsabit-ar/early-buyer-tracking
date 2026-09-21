"""Candidate wallet generation and non-buy filtering module for EBRS.

Implements FR-04 and FR-05 from PRD:
- Filters out non-buy entities (system, DEX programs, burn addresses, creator, self-transfers).
- Extracts candidate early buyer wallets in chronological order of first appearance.
- Initializes idempotent baseline wallet profiles in SQLite.
"""

from typing import FrozenSet, List, Optional, Set

from config import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    KNOWN_DEX_PROGRAMS,
    SYSTEM_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    settings,
)
from models.schemas import ConfidenceEnum, TransferEvent, WalletProfile
from storage.database import Database

# Addresses that should never be treated as buyer candidates (FR-05)
BURN_ADDRESSES: Set[str] = {
    "11111111111111111111111111111111",
    "1nc1nerator11111111111111111111111111111111",
    "deaddeaddeaddeaddeaddeaddeaddeaddeaddeaddead",
}

BLACKLISTED_ADDRESSES: FrozenSet[str] = frozenset(
    {
        SYSTEM_PROGRAM_ID,
        TOKEN_PROGRAM_ID,
        TOKEN_2022_PROGRAM_ID,
        ASSOCIATED_TOKEN_PROGRAM_ID,
    }
    | BURN_ADDRESSES
    | KNOWN_DEX_PROGRAMS
)


def filter_candidate_events(
    transfers: List[TransferEvent],
    creator_address: Optional[str] = None,
) -> List[TransferEvent]:
    """Filter out non-buy transfer events based on blacklisted addresses, creator, and self-transfers.
    
    Args:
        transfers: Chronologically sorted list of TransferEvent models.
        creator_address: Optional token creator/deployer address to filter out.
        
    Returns:
        Filtered list of TransferEvent models.
    """
    filtered: List[TransferEvent] = []
    
    for event in transfers:
        to_addr = event.to_address.strip()
        from_addr = event.from_address.strip()

        # 1. Ignore if recipient is a blacklisted address (DEX, burn, system)
        if to_addr in BLACKLISTED_ADDRESSES:
            continue

        # 2. Ignore if recipient is the token creator/authority (distribution/minting)
        if creator_address and to_addr == creator_address.strip():
            continue

        # 3. Ignore self-transfers
        if to_addr == from_addr:
            continue

        filtered.append(event)

    return filtered


def extract_candidate_wallets(
    filtered_events: List[TransferEvent],
    token_address: Optional[str] = None,
    db: Optional[Database] = None,
) -> List[str]:
    """Extract unique candidate wallets in chronological order of first appearance.
    
    Optionally initializes baseline wallet profiles in the database.
    
    Args:
        filtered_events: Candidate transfer events.
        token_address: Token mint address.
        db: Optional Database instance for persisting baseline profiles.
        
    Returns:
        List of unique candidate wallet addresses (order preserved).
    """
    candidate_wallets: List[str] = []
    seen = set()

    for event in filtered_events:
        wallet = event.to_address.strip()
        if wallet and wallet not in seen:
            seen.add(wallet)
            candidate_wallets.append(wallet)

    # Idempotent baseline profile initialization in SQLite
    if db is not None and candidate_wallets:
        resolved_token = token_address or (
            filtered_events[0].token_address if filtered_events else None
        )
        if resolved_token:
            for wallet in candidate_wallets:
                # Initialize profile if not already present; idempotent via ON CONFLICT
                profile = WalletProfile(
                    wallet_address=wallet,
                    token_address=resolved_token,
                    confidence=ConfidenceEnum.UNKNOWN,
                )
                db.save_wallet_profile(profile)

    return candidate_wallets
