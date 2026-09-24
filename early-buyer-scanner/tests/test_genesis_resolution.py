"""Test suite for bounded and evidence-first genesis time resolution."""

import time
from unittest.mock import MagicMock, patch
import pytest

from collectors.token import resolve_launch_time, fetch_dexscreener_pair_created_at
from models.schemas import ConfidenceEnum, LaunchResolutionType, LaunchTimeResolution, TokenMetadata
from storage.database import Database


@pytest.fixture
def memory_db():
    """Create in-memory Database for testing."""
    db = Database(":memory:")
    db.init_schema()
    return db


def test_exact_genesis_natural_termination(memory_db):
    """Small token: 1 page with <1000 signatures reaches on-chain genesis with HIGH confidence."""
    mock_client = MagicMock()
    # 500 signatures, oldest has blockTime=1700000000
    sigs = [{"signature": f"sig_{i}", "blockTime": 1700000000 + i, "slot": 1000 + i} for i in range(500)]
    sigs.reverse()  # Newest to oldest: sigs[-1] is oldest (blockTime=1700000000)
    mock_client.get_signatures_for_address.return_value = sigs

    mint = "TokenMint1111111111111111111111111111111111"
    res = resolve_launch_time(mint, client=mock_client, db=memory_db, max_pages=5)

    assert isinstance(res, LaunchTimeResolution)
    assert res.launch_time == 1700000000
    assert res.confidence == ConfidenceEnum.HIGH
    assert res.resolution_type == LaunchResolutionType.EXACT_GENESIS.value
    assert res.evidence_signature == "sig_0"
    assert res.pages_fetched == 1
    assert res.signatures_fetched == 500
    assert res.termination_reason == "NATURAL_TERMINATION"

    # Test backward compatibility tuple unpacking
    lt, conf = res
    assert lt == 1700000000
    assert conf == "HIGH"

    # Test indexing and len
    assert res[0] == 1700000000
    assert res[1] == "HIGH"
    assert len(res) == 2

    # Verify persisted in SQLite
    token = memory_db.get_token(mint)
    assert token is not None
    assert token.launch_time == 1700000000
    assert token.launch_confidence == ConfidenceEnum.HIGH


def test_multi_page_natural_termination(memory_db):
    """Medium token: 2 pages where page 2 has <1000 signatures reaches on-chain genesis with HIGH."""
    mock_client = MagicMock()
    batch_1 = [{"signature": f"p1_sig_{i}", "blockTime": 1700001000 + i} for i in range(1000)]
    batch_1.reverse()
    batch_2 = [{"signature": f"p2_sig_{i}", "blockTime": 1700000000 + i} for i in range(250)]
    batch_2.reverse()

    def mock_get_sigs(address, limit=1000, before=None):
        if before is None:
            return batch_1
        elif before == "p1_sig_0":
            return batch_2
        return []

    mock_client.get_signatures_for_address.side_effect = mock_get_sigs

    mint = "TokenMint1111111111111111111111111111111111"
    res = resolve_launch_time(mint, client=mock_client, db=memory_db, max_pages=5)

    assert res.launch_time == 1700000000
    assert res.confidence == ConfidenceEnum.HIGH
    assert res.resolution_type == LaunchResolutionType.EXACT_GENESIS.value
    assert res.evidence_signature == "p2_sig_0"
    assert res.pages_fetched == 2
    assert res.signatures_fetched == 1250
    assert res.termination_reason == "NATURAL_TERMINATION"


def test_bounded_max_pages_with_dexscreener_fallback(memory_db):
    """Large token: Hits max_pages=3; falls back to DexScreener pairCreatedAt with MEDIUM confidence."""
    mock_client = MagicMock()
    # Always returns 1000 signatures per page
    def mock_get_sigs(address, limit=1000, before=None):
        page_idx = 0 if before is None else int(before.split("_")[-1]) + 1
        sigs = [{"signature": f"p{page_idx}_sig_{i}", "blockTime": 1780000000 - (page_idx * 1000) - i} for i in range(1000)]
        sigs[-1]["signature"] = f"cursor_{page_idx}"
        return sigs

    mock_client.get_signatures_for_address.side_effect = mock_get_sigs

    mint = "TokenMint1111111111111111111111111111111111"

    # Mock DexScreener response
    with patch("collectors.token.fetch_dexscreener_pair_created_at") as mock_dex:
        mock_dex.return_value = (1770005500, "raydium", "PairPoolAddress1111111111111111111111111")
        res = resolve_launch_time(mint, client=mock_client, db=memory_db, max_pages=3)

    assert res.launch_time == 1770005500
    assert res.confidence == ConfidenceEnum.MEDIUM
    assert res.resolution_type == LaunchResolutionType.ESTIMATED_POOL_CREATION.value
    assert res.pages_fetched == 3
    assert res.signatures_fetched == 3000
    assert res.termination_reason == "MAX_PAGES_REACHED"
    assert "raydium" in res.evidence_details

    # Persisted in SQLite with MEDIUM confidence
    token = memory_db.get_token(mint)
    assert token.launch_time == 1770005500
    assert token.launch_confidence == ConfidenceEnum.MEDIUM


def test_bounded_max_pages_without_dexscreener_fallback(memory_db):
    """Large token: Hits max_pages=2; DexScreener returns None; falls back to oldest signature with LOW confidence."""
    mock_client = MagicMock()
    page_1 = [{"signature": f"p1_sig_{i}", "blockTime": 1750000000 - i} for i in range(1000)]
    page_1[-1]["signature"] = "cursor_1"
    page_2 = [{"signature": f"p2_sig_{i}", "blockTime": 1740000000 - i} for i in range(1000)]
    page_2[-1] = {"signature": "oldest_seen_sig", "blockTime": 1739999000, "slot": 55555}

    def mock_get_sigs(address, limit=1000, before=None):
        if before is None:
            return page_1
        return page_2

    mock_client.get_signatures_for_address.side_effect = mock_get_sigs

    mint = "TokenMint1111111111111111111111111111111111"

    with patch("collectors.token.fetch_dexscreener_pair_created_at") as mock_dex:
        mock_dex.return_value = None
        res = resolve_launch_time(mint, client=mock_client, db=memory_db, max_pages=2)

    assert res.launch_time == 1739999000
    assert res.confidence == ConfidenceEnum.LOW
    assert res.resolution_type == LaunchResolutionType.BOUNDED_OLDEST_SIGNATURE.value
    assert res.evidence_signature == "oldest_seen_sig"
    assert res.evidence_slot == 55555
    assert res.pages_fetched == 2
    assert res.signatures_fetched == 2000
    assert res.termination_reason == "MAX_PAGES_REACHED"


def test_stagnant_cursor_termination(memory_db):
    """Detects stagnant cursor (RPC returns same last signature) and breaks immediately."""
    mock_client = MagicMock()
    # Batch 1 and Batch 2 both end with signature 'same_cursor'
    batch_1 = [{"signature": f"s_{i}", "blockTime": 1700000000} for i in range(999)]
    batch_1.append({"signature": "same_cursor", "blockTime": 1700000000})

    batch_2 = [{"signature": f"s2_{i}", "blockTime": 1700000000} for i in range(999)]
    batch_2.append({"signature": "same_cursor", "blockTime": 1700000000})

    call_count = 0
    def mock_get_sigs(address, limit=1000, before=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return batch_1
        return batch_2

    mock_client.get_signatures_for_address.side_effect = mock_get_sigs

    mint = "TokenMint1111111111111111111111111111111111"
    with patch("collectors.token.fetch_dexscreener_pair_created_at", return_value=None):
        res = resolve_launch_time(mint, client=mock_client, db=memory_db, max_pages=10)

    assert res.termination_reason == "STAGNANT_CURSOR"
    assert call_count == 2  # Terminated at call 2 without looping to max_pages 10


def test_timeout_termination(memory_db):
    """Stops when max_elapsed_seconds is reached."""
    mock_client = MagicMock()

    def slow_get_sigs(address, limit=1000, before=None):
        time.sleep(0.05)
        return [{"signature": f"s_{i}", "blockTime": 1700000000} for i in range(1000)]

    mock_client.get_signatures_for_address.side_effect = slow_get_sigs

    mint = "TokenMint1111111111111111111111111111111111"
    with patch("collectors.token.fetch_dexscreener_pair_created_at", return_value=None):
        res = resolve_launch_time(
            mint,
            client=mock_client,
            db=memory_db,
            max_pages=100,
            max_elapsed_seconds=0.04,  # Very short timeout
        )

    assert res.termination_reason == "TIMEOUT_REACHED"


def test_sqlite_cache_idempotency_and_force_refresh(memory_db):
    """Returns cached record from SQLite without RPC calls, unless force_refresh=True."""
    mint = "TokenMint1111111111111111111111111111111111"
    memory_db.save_token(
        TokenMetadata(
            token_address=mint,
            launch_time=1700009999,
            launch_confidence=ConfidenceEnum.HIGH,
        )
    )

    mock_client = MagicMock()
    mock_client.get_signatures_for_address.side_effect = RuntimeError("Should not be called!")

    # 1. Cache hit
    res = resolve_launch_time(mint, client=mock_client, db=memory_db, force_refresh=False)
    assert res.launch_time == 1700009999
    assert res.confidence == ConfidenceEnum.HIGH
    assert res.resolution_type == LaunchResolutionType.CACHED_DB.value
    assert res.termination_reason == "CACHE_HIT"
    assert mock_client.get_signatures_for_address.call_count == 0

    # 2. Force refresh bypasses cache
    mock_client.get_signatures_for_address.side_effect = None
    mock_client.get_signatures_for_address.return_value = [
        {"signature": "new_sig", "blockTime": 1700001111}
    ]
    res_refresh = resolve_launch_time(mint, client=mock_client, db=memory_db, force_refresh=True)
    assert res_refresh.launch_time == 1700001111
    assert res_refresh.resolution_type == LaunchResolutionType.EXACT_GENESIS.value
    assert mock_client.get_signatures_for_address.call_count == 1
