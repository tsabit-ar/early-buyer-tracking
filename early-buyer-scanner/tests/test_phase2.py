"""Unit tests for Phase 2: Data Collection & Candidate Generation.

Covers:
1. Solana Base58 address validation.
2. Solscan transfer parsing and pagination.
3. Launch time resolution (HIGH vs LOW confidence).
4. Candidate wallet filtering (FR-05 blacklist, creator, self-transfers).
5. Candidate extraction and database idempotency.
"""

from pathlib import Path
import sys
import pytest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    PUMP_FUN_PROGRAM_ID,
    RAYDIUM_AMM_V4_ID,
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
)
from collectors.token import (
    fetch_token_metadata,
    resolve_launch_time,
    validate_solana_address,
)
from collectors.transfers import collect_historical_transfers
from analyzers.candidate_generator import (
    BLACKLISTED_ADDRESSES,
    extract_candidate_wallets,
    filter_candidate_events,
)
from models.schemas import ConfidenceEnum, TokenMetadata, TransferEvent
from storage.database import Database
from api.solscan import SolscanClient


@pytest.fixture
def memory_db():
    """Fixture providing an in-memory SQLite database instance."""
    db = Database(":memory:")
    yield db
    db.close()


# =====================================================================
# 1. SOLANA ADDRESS VALIDATION TESTS
# =====================================================================

def test_validate_solana_address_valid():
    """Valid Solana Base58 addresses (32-44 chars, no 0, O, I, l)."""
    valid_addresses = [
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
        "So11111111111111111111111111111111111111112", # WSOL
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", # Pump.fun
        "11111111111111111111111111111111",             # System (32 chars)
    ]
    for addr in valid_addresses:
        assert validate_solana_address(addr) == addr


def test_validate_solana_address_invalid():
    """Invalid address checks: forbidden chars (0, O, I, l), wrong lengths, non-strings."""
    invalid_addresses = [
        "",  # Empty
        "short",  # Too short (<32)
        "Toolongaddresswithlengthgreaterthanfortyfourcharacters123456789",  # >44
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt0v",  # Contains '0'
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDtOv",  # Contains 'O'
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDtIv",  # Contains 'I'
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDtlv",  # Contains 'l'
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt!v",  # Special char '!'
    ]
    for addr in invalid_addresses:
        with pytest.raises(ValueError):
            validate_solana_address(addr)

    with pytest.raises(ValueError):
        validate_solana_address(None)  # type: ignore


# =====================================================================
# 2. TOKEN METADATA & LAUNCH TIME RESOLUTION TESTS
# =====================================================================

def test_fetch_token_metadata(memory_db):
    """Verify metadata fetch and persistence to SQLite tokens table."""
    mock_client = MagicMock(spec=SolscanClient)
    mock_client.get_token_meta.return_value = {
        "success": True,
        "data": {
            "address": "TokenMint1111111111111111111111111111111111",
            "name": "Pepe Solana",
            "symbol": "PEPESOL",
            "decimals": 6,
            "creator": "CreatorWallet11111111111111111111111111111111",
        },
    }

    meta = fetch_token_metadata(
        mint_address="TokenMint1111111111111111111111111111111111",
        client=mock_client,
        db=memory_db,
    )

    assert meta.name == "Pepe Solana"
    assert meta.symbol == "PEPESOL"
    assert meta.decimals == 6
    assert meta.creator == "CreatorWallet11111111111111111111111111111111"

    # Verify saved in SQLite
    saved = memory_db.get_token("TokenMint1111111111111111111111111111111111")
    assert saved is not None
    assert saved.symbol == "PEPESOL"


def test_resolve_launch_time_success_high_confidence(memory_db):
    """When earliest transfer exists, launch_time is set with HIGH confidence."""
    mock_client = MagicMock(spec=SolscanClient)
    mock_client.get_token_transfers.return_value = {
        "success": True,
        "data": [
            {
                "signature": "sig_genesis",
                "block_time": 1715000000,
                "from_address": "Deployer11111111111111111111111111111111111",
                "to_address": "Pool1111111111111111111111111111111111111111",
                "amount": 1000000000,
            }
        ],
    }

    mint = "TokenMint1111111111111111111111111111111111"
    launch_time, confidence = resolve_launch_time(mint, client=mock_client, db=memory_db)

    assert launch_time == 1715000000
    assert confidence == ConfidenceEnum.HIGH.value

    # Verify database updated
    db_token = memory_db.get_token(mint)
    assert db_token is not None
    assert db_token.launch_time == 1715000000
    assert db_token.launch_confidence == ConfidenceEnum.HIGH


def test_resolve_launch_time_empty_low_confidence(memory_db):
    """When no transfers exist, launch_time is None with LOW confidence."""
    mock_client = MagicMock(spec=SolscanClient)
    mock_client.get_token_transfers.return_value = {"success": True, "data": []}

    mint = "TokenMint1111111111111111111111111111111111"
    launch_time, confidence = resolve_launch_time(mint, client=mock_client, db=memory_db)

    assert launch_time is None
    assert confidence == ConfidenceEnum.LOW.value

    db_token = memory_db.get_token(mint)
    assert db_token is not None
    assert db_token.launch_time is None
    assert db_token.launch_confidence == ConfidenceEnum.LOW


# =====================================================================
# 3. HISTORICAL TRANSFERS COLLECTION TESTS
# =====================================================================

def test_collect_historical_transfers_pagination_and_storage(memory_db):
    """Verify pagination loop stops at max_transfers and stores records in SQLite."""
    mock_client = MagicMock(spec=SolscanClient)

    # Page 1 returns 2 transfers
    page_1_data = {
        "data": [
            {
                "trans_id": "tx1",
                "block_time": 1715000010,
                "from_address": "FromA11111111111111111111111111111111111",
                "to_address": "ToB1111111111111111111111111111111111111",
                "amount": 5000.0,
                "decimals": 9,
            },
            {
                "trans_id": "tx2",
                "block_time": 1715000020,
                "from_address": "FromA11111111111111111111111111111111111",
                "to_address": "ToC1111111111111111111111111111111111111",
                "amount": 10000.0,
                "decimals": 9,
            },
        ]
    }
    # Page 2 returns 1 transfer
    page_2_data = {
        "data": [
            {
                "trans_id": "tx3",
                "block_time": 1715000030,
                "from_address": "FromA11111111111111111111111111111111111",
                "to_address": "ToD1111111111111111111111111111111111111",
                "amount": 2000.0,
                "decimals": 9,
            }
        ]
    }
    # Page 3 returns empty
    page_3_data = {"data": []}

    mock_client.get_token_transfers.side_effect = [page_1_data, page_2_data, page_3_data]

    mint = "TokenMint1111111111111111111111111111111111"
    transfers = collect_historical_transfers(
        mint_address=mint,
        max_transfers=5,
        page_size=2,
        client=mock_client,
        db=memory_db,
    )

    assert len(transfers) == 3
    assert transfers[0].signature == "tx1"
    assert transfers[1].signature == "tx2"
    assert transfers[2].signature == "tx3"
    assert mock_client.get_token_transfers.call_count == 3

    # Check persistence in wallet_events table
    events_b = memory_db.get_wallet_events("ToB1111111111111111111111111111111111111", mint)
    assert len(events_b) == 1
    assert events_b[0]["signature"] == "tx1"
    assert events_b[0]["amount"] == 5000.0


# =====================================================================
# 4. CANDIDATE FILTERING & EXTRACTION TESTS
# =====================================================================

def test_filter_candidate_events():
    """Verify known non-buy addresses (DEX, Burn, System, Creator, Self-transfers) are filtered out."""
    creator = "CreatorWallet11111111111111111111111111111111"
    mint = "TokenMint1111111111111111111111111111111111"

    events = [
        # 1. Valid buyer candidate
        TransferEvent(
            signature="tx_buyer1",
            block_time=100,
            from_address="Pool1111111111111111111111111111111111111111",
            to_address="BuyerWalletA11111111111111111111111111111111",
            token_address=mint,
            amount=1000.0,
        ),
        # 2. Transfer to Raydium AMM (DEX program) -> MUST BE FILTERED
        TransferEvent(
            signature="tx_raydium",
            block_time=101,
            from_address="BuyerWalletA11111111111111111111111111111111",
            to_address=RAYDIUM_AMM_V4_ID,
            token_address=mint,
            amount=500.0,
        ),
        # 3. Transfer to Pump.fun Program -> MUST BE FILTERED
        TransferEvent(
            signature="tx_pump",
            block_time=102,
            from_address="BuyerWalletA11111111111111111111111111111111",
            to_address=PUMP_FUN_PROGRAM_ID,
            token_address=mint,
            amount=500.0,
        ),
        # 4. Transfer to Burn address -> MUST BE FILTERED
        TransferEvent(
            signature="tx_burn",
            block_time=103,
            from_address="BuyerWalletA11111111111111111111111111111111",
            to_address="11111111111111111111111111111111",
            token_address=mint,
            amount=100.0,
        ),
        # 5. Transfer to Creator -> MUST BE FILTERED
        TransferEvent(
            signature="tx_creator",
            block_time=104,
            from_address="BuyerWalletA11111111111111111111111111111111",
            to_address=creator,
            token_address=mint,
            amount=100.0,
        ),
        # 6. Self-transfer -> MUST BE FILTERED
        TransferEvent(
            signature="tx_self",
            block_time=105,
            from_address="BuyerWalletB11111111111111111111111111111111",
            to_address="BuyerWalletB11111111111111111111111111111111",
            token_address=mint,
            amount=50.0,
        ),
        # 7. Another valid buyer candidate
        TransferEvent(
            signature="tx_buyer2",
            block_time=106,
            from_address="Pool1111111111111111111111111111111111111111",
            to_address="BuyerWalletB11111111111111111111111111111111",
            token_address=mint,
            amount=2000.0,
        ),
    ]

    filtered = filter_candidate_events(events, creator_address=creator)

    assert len(filtered) == 2
    assert filtered[0].signature == "tx_buyer1"
    assert filtered[1].signature == "tx_buyer2"
    assert filtered[0].to_address == "BuyerWalletA11111111111111111111111111111111"
    assert filtered[1].to_address == "BuyerWalletB11111111111111111111111111111111"


def test_extract_candidate_wallets_and_idempotency(memory_db):
    """Verify unique candidate extraction order and idempotent database writes."""
    mint = "TokenMint1111111111111111111111111111111111"
    events = [
        TransferEvent(
            signature="sig1",
            block_time=100,
            from_address="Pool",
            to_address="Wallet111111111111111111111111111111111111111",
            token_address=mint,
            amount=100.0,
        ),
        TransferEvent(
            signature="sig2",
            block_time=105,
            from_address="Pool",
            to_address="Wallet222222222222222222222222222222222222222",
            token_address=mint,
            amount=200.0,
        ),
        TransferEvent(
            signature="sig3",
            block_time=110,
            from_address="Pool",
            to_address="Wallet111111111111111111111111111111111111111",  # Repeated
            token_address=mint,
            amount=300.0,
        ),
    ]

    # First run
    candidates1 = extract_candidate_wallets(events, token_address=mint, db=memory_db)
    assert len(candidates1) == 2
    assert candidates1[0] == "Wallet111111111111111111111111111111111111111"
    assert candidates1[1] == "Wallet222222222222222222222222222222222222222"

    profiles_after_run1 = memory_db.get_wallet_profiles(mint)
    assert len(profiles_after_run1) == 2

    # Second run (Idempotency test)
    candidates2 = extract_candidate_wallets(events, token_address=mint, db=memory_db)
    assert candidates2 == candidates1

    profiles_after_run2 = memory_db.get_wallet_profiles(mint)
    assert len(profiles_after_run2) == 2  # No duplicate rows created!
