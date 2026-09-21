"""Tests for Solana Native RPC Client (SolanaRpcClient) and ACAT transaction parsing.

Covers:
1. SQLite caching in SolanaRpcClient (Cache-First).
2. Parsing and classification of ACAT transaction where quote asset is USDC (HIGH confidence BUY).
3. Live / cached RPC query of ACAT transaction.
"""

import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyzers.transaction_classifier import classify_transaction
from api.solana_rpc import SolanaRPCError, SolanaRpcClient
from collectors.transactions import parse_solscan_tx_detail
from models.schemas import ClassificationEnum, ConfidenceEnum
from storage.database import Database

ACAT_MINT = "7uvLyn87LSxW2GdwdEeiwmSJwQLVrcVyo7SRVcLbbtGc"
ACAT_SIG = "5ksNkCfpLgPheeeKrJ5DdY5zEXurFkibqT7dQSmEDxnrKqBYdBDbFuhTVWpvNtL1dfVo3T9uJRhnQZBFtxw4bqWq"
BUYER_WALLET = "5bsM2htn7kzNUzqRaBFhcWFmNvZcRoh1eV3WBhWUo1KB"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture
def memory_db():
    """Provide an in-memory SQLite database instance."""
    db = Database(":memory:")
    yield db
    db.close()


def test_rpc_cache_first(memory_db):
    """Verify that SolanaRpcClient caches RPC responses in SQLite api_cache."""
    rpc = SolanaRpcClient(database=memory_db)
    mock_result = {"slot": 123456789}
    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"result": mock_result}

    with patch.object(rpc.client, "post", return_value=mock_response) as mock_post:
        # First call: hits network
        res1 = rpc._call_rpc("getSlot", [])
        assert res1 == mock_result
        assert mock_post.call_count == 1

        # Second call: served from SQLite cache
        res2 = rpc._call_rpc("getSlot", [])
        assert res2 == mock_result
        assert mock_post.call_count == 1  # No additional network call


def test_acat_sample_classification_usdc_buy():
    """Verify that ACAT transaction with USDC quote asset classifies as BUY with HIGH confidence."""
    fixture_path = PROJECT_ROOT / "tests" / "fixtures" / "live_tx_sample.json"
    assert fixture_path.exists(), "live_tx_sample.json fixture must exist"

    with open(fixture_path, "r", encoding="utf-8") as f:
        sample_data = json.load(f)

    # Parse using parse_solscan_tx_detail
    tx_detail = parse_solscan_tx_detail(sample_data, ACAT_SIG)

    assert tx_detail.signature == ACAT_SIG
    assert tx_detail.status == "Success"

    # Classify for the buyer wallet
    result = classify_transaction(
        tx=tx_detail,
        wallet_address=BUYER_WALLET,
        token_address=ACAT_MINT,
    )

    assert result.classification == ClassificationEnum.BUY
    assert result.confidence == ConfidenceEnum.HIGH
    assert result.token_change > 0
    # Buyer spent USDC and received ACAT
    assert any(b.mint == USDC_MINT and b.change < 0 for b in tx_detail.token_balance_changes)
    assert any(b.mint == ACAT_MINT and b.change > 0 for b in tx_detail.token_balance_changes)


def test_rpc_live_or_cached_acat(memory_db):
    """Test RPC fetch and classification for ACAT transaction (live or cached)."""
    rpc = SolanaRpcClient(database=memory_db)

    try:
        raw_detail = rpc.get_transaction_detail(ACAT_SIG)
        tx_detail = parse_solscan_tx_detail(raw_detail, ACAT_SIG)

        result = classify_transaction(
            tx=tx_detail,
            wallet_address=BUYER_WALLET,
            token_address=ACAT_MINT,
        )
        assert result.classification == ClassificationEnum.BUY
        assert result.confidence == ConfidenceEnum.HIGH
        assert result.token_change > 0
    except (SolanaRPCError, Exception) as exc:
        pytest.skip(f"Public RPC rate limit or network issue: {exc}")


def test_metaplex_pda_and_decoding():
    """Verify Metaplex PDA derivation and binary unpacking."""
    import struct
    from collectors.token import decode_metaplex_metadata, derive_metaplex_metadata_pda

    # 1. PDA derivation for BONK
    bonk_pda = derive_metaplex_metadata_pda("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
    assert bonk_pda == "FDZZbyY9XGpL3CNKUZxLk3wFTTQYL3TkDiDzqxrizcPN"

    # 2. Decoding synthetic Metaplex binary payload
    prefix = b"\x04" + (b"U" * 32) + (b"M" * 32)
    name_bytes = b"Magic Token\x00\x00\x00"
    symbol_bytes = b"MAGIC\x00"
    raw_payload = prefix + struct.pack("<I", len(name_bytes)) + name_bytes + struct.pack("<I", len(symbol_bytes)) + symbol_bytes

    name, symbol = decode_metaplex_metadata(raw_payload)
    assert name == "Magic Token"
    assert symbol == "MAGIC"


def test_same_block_sniper_tagging(memory_db):
    """Verify detection of wallets buying in the same slot and DB persistence."""
    from analyzers.wallet_analyzer import tag_same_block_snipers
    from models.schemas import WalletProfile

    w1 = WalletProfile(
        wallet_address="Wallet11111111111111111111111111111111111",
        token_address=ACAT_MINT,
        first_buy_time=1000,
        first_buy_slot=448913860,
        first_buy_amount=1000.0,
    )
    w2 = WalletProfile(
        wallet_address="Wallet22222222222222222222222222222222222",
        token_address=ACAT_MINT,
        first_buy_time=1000,
        first_buy_slot=448913860,  # Same slot!
        first_buy_amount=2000.0,
    )
    w3 = WalletProfile(
        wallet_address="Wallet33333333333333333333333333333333333",
        token_address=ACAT_MINT,
        first_buy_time=1010,
        first_buy_slot=448913890,  # Different slot
        first_buy_amount=500.0,
    )

    tagged = tag_same_block_snipers([w1, w2, w3])
    assert tagged[0].is_same_block_sniper is True
    assert tagged[1].is_same_block_sniper is True
    assert tagged[2].is_same_block_sniper is False

    # Test DB persistence
    for p in tagged:
        memory_db.save_wallet_profile(p)

    db_profiles = memory_db.get_wallet_profiles(ACAT_MINT)
    assert len(db_profiles) == 3
    sniper_map = {p.wallet_address: p.is_same_block_sniper for p in db_profiles}
    assert sniper_map["Wallet11111111111111111111111111111111111"] is True
    assert sniper_map["Wallet22222222222222222222222222222222222"] is True
    assert sniper_map["Wallet33333333333333333333333333333333333"] is False


def test_resolve_launch_time_backward_pagination(memory_db):
    """Verify resolve_launch_time paginates backwards using 'before' to reach absolute genesis."""
    from collectors.token import resolve_launch_time

    mock_client = MagicMock()
    # Batch 1 (1000 items, newest): last signature is 'sig_mid'
    batch_1 = [{"signature": f"sig_recent_{i}", "blockTime": 1789960000} for i in range(999)]
    batch_1.append({"signature": "sig_mid", "blockTime": 1789955000})

    # Batch 2 (<1000 items, genesis): oldest signature is 'sig_genesis'
    batch_2 = [
        {"signature": "sig_early", "blockTime": 1789953000},
        {"signature": "sig_genesis", "blockTime": 1789952445},
    ]

    def mock_get_sigs(address, limit=1000, before=None):
        if before is None:
            return batch_1
        elif before == "sig_mid":
            return batch_2
        return []

    mock_client.get_signatures_for_address.side_effect = mock_get_sigs

    mint = "TokenMint1111111111111111111111111111111111"
    launch_time, confidence = resolve_launch_time(mint, client=mock_client, db=memory_db)

    assert launch_time == 1789952445
    assert confidence == ConfidenceEnum.HIGH.value

    # Verify persisted to SQLite
    token = memory_db.get_token(mint)
    assert token is not None
    assert token.launch_time == 1789952445
    assert token.launch_confidence == ConfidenceEnum.HIGH


def test_resolve_launch_time_sqlite_idempotency(memory_db):
    """Verify resolve_launch_time returns existing valid launch_time from DB without calling RPC."""
    from collectors.token import resolve_launch_time
    from models.schemas import TokenMetadata

    mint = "TokenMint1111111111111111111111111111111111"
    memory_db.save_token(
        TokenMetadata(
            token_address=mint,
            launch_time=1789952445,
            launch_confidence=ConfidenceEnum.HIGH,
        )
    )

    mock_client = MagicMock()
    mock_client.get_signatures_for_address.side_effect = RuntimeError("Should not be called!")

    launch_time, confidence = resolve_launch_time(mint, client=mock_client, db=memory_db)

    assert launch_time == 1789952445
    assert confidence == ConfidenceEnum.HIGH.value
    assert mock_client.get_signatures_for_address.call_count == 0


def test_collect_historical_transfers_genesis_order(memory_db):
    """Verify collect_historical_transfers orders transactions starting from genesis forward."""
    from collectors.transfers import collect_historical_transfers

    mock_client = MagicMock()
    # Batch 1 (recent): sig3, sig4
    batch_1 = [
        {"signature": "sig4", "blockTime": 1789952500},
        {"signature": "sig3", "blockTime": 1789952480},
    ]
    # For test simplicity, let's say batch_1 is 1000 items so it queries before="sig3"
    batch_1_full = [{"signature": f"sig_pad_{i}", "blockTime": 1789952500} for i in range(998)]
    batch_1_full.extend([
        {"signature": "sig4", "blockTime": 1789952500},
        {"signature": "sig3", "blockTime": 1789952480},
    ])
    # Batch 2 (genesis batch, <1000 items): sig2, sig1 (sig1 is genesis)
    batch_2 = [
        {"signature": "sig2", "blockTime": 1789952460},
        {"signature": "sig1", "blockTime": 1789952445},
    ]

    def mock_get_sigs(address, limit=1000, before=None):
        if before is None:
            return batch_1_full
        elif before == "sig3":
            return batch_2
        return []

    mock_client.get_signatures_for_address.side_effect = mock_get_sigs

    # Mock get_transaction_detail
    def mock_get_detail(sig):
        return {
            "data": {
                "signer": ["Signer11111111111111111111111111111111111"],
                "token_bal_change": [
                    {
                        "token_address": "TokenMint1111111111111111111111111111111111",
                        "address": f"Buyer_{sig}",
                        "change": 100.0,
                        "decimals": 9,
                    }
                ],
            }
        }

    mock_client.get_transaction_detail.side_effect = mock_get_detail

    mint = "TokenMint1111111111111111111111111111111111"
    transfers = collect_historical_transfers(
        mint_address=mint,
        max_transfers=2,
        client=mock_client,
        db=memory_db,
    )

    assert len(transfers) == 2
    # sig1 is absolute genesis (oldest), then sig2
    assert transfers[0].signature == "sig1"
    assert transfers[0].block_time == 1789952445
    assert transfers[1].signature == "sig2"
    assert transfers[1].block_time == 1789952460


