"""Foundation tests for Early Buyer Scanner (EBRS) Phase 1.

Covers:
1. Pydantic v2 schemas validation.
2. SQLite schema initialization and atomic transactions (commit & rollback).
3. Deterministic SHA-256 caching get/set operations.
4. SolscanClient resilience (Cache-first pattern, 429 backoff retry, client error handling).
"""

from pathlib import Path
import sys
import pytest
from unittest.mock import MagicMock, patch
import httpx

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    KNOWN_DEX_PROGRAMS,
    PUMP_FUN_PROGRAM_ID,
    RAYDIUM_AMM_V4_ID,
    WSOL_MINT,
    settings,
)
from models.schemas import (
    BalanceChange,
    ClassificationEnum,
    ConfidenceEnum,
    ScannerReport,
    ScoreBreakdown,
    TokenMetadata,
    TransactionClassification,
    TransactionDetail,
    TransferEvent,
    WalletProfile,
)
from storage.database import Database, compute_cache_key
from api.solscan import SolscanAPIError, SolscanClient


# =====================================================================
# 1. PYDANTIC V2 SCHEMA VALIDATION TESTS
# =====================================================================

def test_token_metadata_validation():
    """Verify TokenMetadata fields, defaults, and enum serialization."""
    token = TokenMetadata(
        token_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        name="USD Coin",
        symbol="USDC",
        decimals=6,
        creator="2WMvCSbmum4jvqHGV3vFJp2355555555555555555555",
        launch_time=1620000000,
        launch_confidence=ConfidenceEnum.HIGH,
    )
    assert token.token_address == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    assert token.decimals == 6
    assert token.launch_confidence == ConfidenceEnum.HIGH
    assert token.created_at is not None

    # Invalid decimals check (must be <= 18)
    with pytest.raises(Exception):
        TokenMetadata(
            token_address="Test111111111111111111111111111111111111111",
            decimals=25,
        )


def test_transfer_event_validation():
    """Verify TransferEvent structure and non-negative amounts."""
    transfer = TransferEvent(
        signature="5K3e...xyz",
        block_time=1700000000,
        from_address="Sender1111111111111111111111111111111111111",
        to_address="Receiver111111111111111111111111111111111111",
        token_address="TokenMint1111111111111111111111111111111111",
        amount=150000.5,
        decimals=9,
    )
    assert transfer.amount == 150000.5
    assert transfer.decimals == 9

    # Negative amount should fail
    with pytest.raises(Exception):
        TransferEvent(
            signature="sig123",
            block_time=1700000000,
            from_address="A",
            to_address="B",
            token_address="C",
            amount=-50.0,
        )


def test_balance_change_and_transaction_detail():
    """Verify BalanceChange and TransactionDetail composition."""
    sol_change = BalanceChange(
        address="BuyerWallet1111111111111111111111111111111",
        pre_balance=5.0,
        post_balance=3.5,
        change=-1.5,
        mint=None,
        decimals=9,
    )
    token_change = BalanceChange(
        address="BuyerWallet1111111111111111111111111111111",
        pre_balance=0.0,
        post_balance=1000000.0,
        change=1000000.0,
        mint="TokenMint1111111111111111111111111111111111",
        decimals=9,
    )

    tx_detail = TransactionDetail(
        signature="txSig123456",
        block_time=1700000100,
        signer="BuyerWallet1111111111111111111111111111111",
        sol_balance_changes=[sol_change],
        token_balance_changes=[token_change],
        programs=[PUMP_FUN_PROGRAM_ID],
        status="Success",
        priority_fee=0.00005,
    )

    assert tx_detail.signer == "BuyerWallet1111111111111111111111111111111"
    assert len(tx_detail.sol_balance_changes) == 1
    assert tx_detail.sol_balance_changes[0].change == -1.5
    assert tx_detail.programs[0] == PUMP_FUN_PROGRAM_ID


def test_transaction_classification_model():
    """Verify TransactionClassification model conforms to FR-07."""
    tx_class = TransactionClassification(
        signature="sig789",
        wallet="BuyerWallet1111111111111111111111111111111",
        token_address="TokenMint1111111111111111111111111111111111",
        block_time=1700000100,
        classification=ClassificationEnum.BUY,
        confidence=ConfidenceEnum.HIGH,
        reasons=["SOL spent: 1.5", "Token received: 1,000,000", "Pump.fun program invoked"],
        sol_change=-1.5,
        token_change=1000000.0,
        programs=[PUMP_FUN_PROGRAM_ID],
    )
    assert tx_class.classification == ClassificationEnum.BUY
    assert tx_class.confidence == ConfidenceEnum.HIGH
    assert len(tx_class.reasons) == 3


def test_wallet_profile_exit_ratio_and_scoring():
    """Verify WalletProfile fields and exit_ratio clamp/rounding."""
    profile = WalletProfile(
        wallet_address="BuyerWallet1111111111111111111111111111111",
        token_address="TokenMint1111111111111111111111111111111111",
        first_buy_time=1700000100,
        first_buy_signature="sig789",
        first_buy_amount=1000000.0,
        time_after_launch=134,
        total_buy_amount=1000000.0,
        buy_count=1,
        sell_count=0,
        total_sell_amount=0.0,
        current_holding=1000000.0,
        exit_ratio=0.0,
        score=91.5,
        confidence=ConfidenceEnum.HIGH,
    )
    assert profile.exit_ratio == 0.0
    assert profile.score == 91.5

    breakdown = ScoreBreakdown(
        wallet_address=profile.wallet_address,
        early_entry_score=90.0,
        buy_size_score=95.0,
        accumulation_score=50.0,
        holding_score=100.0,
        final_score=91.5,
        breakdown_details={"time_after_launch": 134},
    )
    assert breakdown.final_score == 91.5

    report = ScannerReport(
        token=TokenMetadata(token_address="TokenMint1111111111111111111111111111111111"),
        candidates_count=10,
        likely_buyers_count=1,
        buyers=[profile],
    )
    assert report.likely_buyers_count == 1
    assert len(report.buyers) == 1


# =====================================================================
# 2. SQLITE SCHEMA & ATOMIC TRANSACTION TESTS
# =====================================================================

@pytest.fixture
def memory_db():
    """Fixture providing an in-memory SQLite database instance."""
    db = Database(":memory:")
    yield db
    db.close()


def test_database_table_initialization(memory_db):
    """Ensure all required tables and indices are created."""
    conn = memory_db.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row["name"] for row in cursor.fetchall()}

    expected_tables = {
        "tokens",
        "transactions",
        "wallet_profiles",
        "wallet_events",
        "api_cache",
    }
    assert expected_tables.issubset(tables)


def test_atomic_transaction_commit_and_rollback(memory_db):
    """Test that context manager commits on success and rolls back on exception."""
    # Successful transaction
    with memory_db.transaction() as cursor:
        cursor.execute(
            "INSERT INTO tokens (token_address, name, symbol) VALUES (?, ?, ?);",
            ("MintA", "Token A", "TKNA"),
        )

    # Verify committed
    assert memory_db.get_token("MintA") is not None

    # Failed transaction -> should rollback
    with pytest.raises(RuntimeError):
        with memory_db.transaction() as cursor:
            cursor.execute(
                "INSERT INTO tokens (token_address, name, symbol) VALUES (?, ?, ?);",
                ("MintB", "Token B", "TKNB"),
            )
            raise RuntimeError("Simulated failure during insert")

    # Verify MintB was rolled back
    assert memory_db.get_token("MintB") is None


def test_database_crud_operations(memory_db):
    """Test CRUD operations for tokens, transactions, profiles, and events."""
    # Token CRUD
    token = TokenMetadata(
        token_address="So11111111111111111111111111111111111111112",
        name="Wrapped SOL",
        symbol="WSOL",
        decimals=9,
        launch_confidence=ConfidenceEnum.HIGH,
    )
    memory_db.save_token(token)
    fetched_token = memory_db.get_token(token.token_address)
    assert fetched_token is not None
    assert fetched_token.symbol == "WSOL"
    assert fetched_token.launch_confidence == ConfidenceEnum.HIGH

    # Transaction CRUD
    tx = TransactionClassification(
        signature="tx_test_001",
        wallet="WalletA",
        token_address=token.token_address,
        block_time=1700000000,
        classification=ClassificationEnum.BUY,
        confidence=ConfidenceEnum.HIGH,
        reasons=["Test reason"],
        sol_change=-2.0,
        token_change=50000.0,
        programs=[RAYDIUM_AMM_V4_ID],
    )
    assert not memory_db.has_transaction("tx_test_001")
    memory_db.save_transaction(tx, raw_data={"sample": 123})
    assert memory_db.has_transaction("tx_test_001")
    fetched_tx = memory_db.get_transaction("tx_test_001")
    assert fetched_tx["classification"] == "BUY"
    assert fetched_tx["sol_change"] == -2.0
    assert fetched_tx["programs"] == [RAYDIUM_AMM_V4_ID]
    assert fetched_tx["raw_data"] == {"sample": 123}

    # Wallet profile CRUD
    profile = WalletProfile(
        wallet_address="WalletA",
        token_address=token.token_address,
        first_buy_time=1700000000,
        first_buy_signature="tx_test_001",
        first_buy_amount=50000.0,
        time_after_launch=60,
        total_buy_amount=50000.0,
        buy_count=1,
        score=88.0,
        confidence=ConfidenceEnum.HIGH,
    )
    memory_db.save_wallet_profile(profile)
    profiles = memory_db.get_wallet_profiles(token.token_address)
    assert len(profiles) == 1
    assert profiles[0].wallet_address == "WalletA"
    assert profiles[0].score == 88.0

    # Wallet events CRUD
    memory_db.save_wallet_event(
        wallet_address="WalletA",
        token_address=token.token_address,
        signature="tx_test_001",
        timestamp=1700000000,
        event_type="BUY",
        amount=50000.0,
        quote_amount=2.0,
        confidence="HIGH",
    )
    events = memory_db.get_wallet_events("WalletA", token.token_address)
    assert len(events) == 1
    assert events[0]["event_type"] == "BUY"
    assert events[0]["amount"] == 50000.0


# =====================================================================
# 3. DETERMINISTIC CACHE HASHING & GET/SET TESTS
# =====================================================================

def test_deterministic_cache_key_computation():
    """Verify compute_cache_key is deterministic regardless of param dict key ordering."""
    endpoint = "token/transfer"
    params1 = {"address": "Mint123", "page": 1, "page_size": 100, "sort_order": "asc"}
    params2 = {"sort_order": "asc", "page_size": 100, "address": "Mint123", "page": 1}

    key1 = compute_cache_key(endpoint, params1)
    key2 = compute_cache_key(endpoint, params2)

    assert key1 == key2
    assert len(key1) == 64  # SHA-256 hex length

    # Empty params vs None params
    key_empty = compute_cache_key("token/meta", {})
    key_none = compute_cache_key("token/meta", None)
    assert key_empty == key_none


def test_api_cache_get_set(memory_db):
    """Verify storing and retrieving responses in api_cache."""
    endpoint = "token/meta"
    params = {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
    cache_key = compute_cache_key(endpoint, params)

    assert memory_db.get_cached_response(cache_key) is None

    payload = {"success": True, "data": {"name": "USD Coin", "symbol": "USDC"}}
    memory_db.set_cached_response(cache_key, endpoint, params, payload)

    cached = memory_db.get_cached_response(cache_key)
    assert cached is not None
    assert cached["success"] is True
    assert cached["data"]["symbol"] == "USDC"


# =====================================================================
# 4. SOLSCAN CLIENT CACHING & RESILIENCE (429 BACKOFF) TESTS
# =====================================================================

def test_solscan_client_cache_hit(memory_db):
    """Verify that if data is already in SQLite cache, no network request is made."""
    endpoint = "token/meta"
    params = {"address": "TokenABC"}
    cache_key = compute_cache_key(endpoint, params)

    cached_payload = {"cached": True, "mint": "TokenABC"}
    memory_db.set_cached_response(cache_key, endpoint, params, cached_payload)

    client = SolscanClient(
        api_key="test-api-key",
        database=memory_db,
        rate_limit_rps=100.0,
    )

    with patch.object(client.client, "get") as mock_get:
        res = client.get_token_meta("TokenABC")
        # Ensure client.get was never called because it hit cache
        mock_get.assert_not_called()
        assert res == cached_payload


def test_solscan_client_network_fetch_and_cache_store(memory_db):
    """Verify that network fetch on cache miss populates SQLite cache."""
    client = SolscanClient(
        api_key="test-api-key",
        database=memory_db,
        rate_limit_rps=100.0,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"success": True, "transfers": []}

    with patch.object(client.client, "get", return_value=mock_resp) as mock_get:
        res1 = client.get_token_transfers("TokenABC", page=1, page_size=10)
        assert mock_get.call_count == 1
        assert res1["success"] is True

        # Second call with same arguments must hit cache without network get
        res2 = client.get_token_transfers("TokenABC", page=1, page_size=10)
        assert mock_get.call_count == 1  # count did not increase
        assert res2 == res1


def test_solscan_client_429_backoff_and_retry(memory_db):
    """Verify that HTTP 429 triggers exponential backoff and retries until success."""
    client = SolscanClient(
        api_key="test-api-key",
        database=memory_db,
        max_retries=3,
        rate_limit_rps=100.0,
    )

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"Retry-After": "0.01"}
    resp_429.text = "Too Many Requests"

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {"tx": "sig123", "status": "Success"}

    # Simulate: 429 on first call, 200 on second call
    with patch.object(client.client, "get", side_effect=[resp_429, resp_200]) as mock_get:
        with patch("time.sleep") as mock_sleep:
            res = client.get_transaction_detail("sig123")
            assert mock_get.call_count == 2
            assert res["status"] == "Success"
            mock_sleep.assert_called()


def test_solscan_client_max_retries_exceeded(memory_db):
    """Verify that exhausting retries on 429 raises SolscanAPIError."""
    client = SolscanClient(
        api_key="test-api-key",
        database=memory_db,
        max_retries=2,
        rate_limit_rps=100.0,
    )

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {}
    resp_429.text = "Rate Limited"

    with patch.object(client.client, "get", return_value=resp_429):
        with patch("time.sleep"):
            with pytest.raises(SolscanAPIError) as exc_info:
                client.get_token_meta("RateLimitedToken")
            assert exc_info.value.status_code == 429


def test_solscan_client_non_retryable_error(memory_db):
    """Verify client errors (e.g. 401 Unauthorized, 404 Not Found) fail immediately."""
    client = SolscanClient(
        api_key="invalid-api-key",
        database=memory_db,
        max_retries=3,
        rate_limit_rps=100.0,
    )

    resp_401 = MagicMock()
    resp_401.status_code = 401
    resp_401.text = "Unauthorized"

    with patch.object(client.client, "get", return_value=resp_401) as mock_get:
        with pytest.raises(SolscanAPIError) as exc_info:
            client.get_token_meta("TokenXYZ")
        assert mock_get.call_count == 1  # failed immediately without retrying
        assert exc_info.value.status_code == 401
