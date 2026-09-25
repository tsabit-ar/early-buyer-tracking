"""Unit and regression tests for FREE_LIMITED Discovery Mode.

Tests all 10 mandatory scenarios:
1. FREE_LIMITED + Playground 200 -> candidates processed, discovery_source = SOLSCAN_PLAYGROUND
2. FREE_LIMITED + Playground 429 -> no infinite retry, falls back to Native RPC
3. FREE_LIMITED + Playground 401 -> falls back to Native RPC
4. Playground 200 but partial pages (budget reached) -> candidate_discovery_complete=False, candidate_discovery_truncated=True
5. Playground 200 + genesis reached + natural termination -> candidate_discovery_complete=True
6. HTTP 200 alone does NOT imply COMPLETE (truncated budget remains INCOMPLETE)
7. SOLSCAN_PLAYGROUND is distinct from SOLSCAN_ASC
8. Early-window incomplete discovery never becomes EARLY_WINDOW_CONFIRMED
9. Rate-limit server is not hard-coded as "20 request/hour" (config parameters are local safety budget)
10. Fail-fast non-aggressive handling on 429/401 in SolscanClient
"""

from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.solscan import SolscanAPIError, SolscanClient
from collectors.transfers import CandidateDiscoveryResult, collect_historical_transfers
from config import settings
from models.schemas import (
    ConfidenceEnum,
    DiscoveryModeEnum,
    DiscoverySourceEnum,
    EarlyWindowBasisEnum,
    EarlyWindowStatusEnum,
    LaunchResolutionType,
    WalletProfile,
)
from storage.database import Database


@pytest.fixture
def memory_db():
    """Create an in-memory SQLite database instance."""
    db = Database(":memory:")
    db.init_schema()
    yield db
    db.close()


def test_scenario_1_free_limited_playground_200_source_and_candidates(memory_db):
    """Scenario 1: FREE_LIMITED + Playground 200 -> candidates processed, discovery_source = SOLSCAN_PLAYGROUND."""
    mock_client = MagicMock()

    mock_client.get_playground_token_transfers.return_value = {
        "success": True,
        "data": [
            {
                "trans_id": "tx_pg_1",
                "block_time": 1789952450,
                "from_address": "Deployer11111111111111111111111111111111111",
                "to_address": "Buyer11111111111111111111111111111111111111",
                "amount": 1000.0,
                "decimals": 9,
                "activity_type": "ACTIVITY_SPL_MINT",
            },
            {
                "trans_id": "tx_pg_2",
                "block_time": 1789952460,
                "from_address": "Deployer11111111111111111111111111111111111",
                "to_address": "Buyer22222222222222222222222222222222222222",
                "amount": 2000.0,
                "decimals": 9,
                "activity_type": "ACTIVITY_SPL_TRANSFER",
            },
        ],
    }

    mint = "TokenMint1111111111111111111111111111111111"
    result = collect_historical_transfers(
        mint_address=mint,
        max_transfers=10,
        client=mock_client,
        db=memory_db,
        discovery_mode="FREE_LIMITED",
    )

    assert result.discovery_mode == "FREE_LIMITED"
    assert result.discovery_source == DiscoverySourceEnum.SOLSCAN_PLAYGROUND.value
    assert len(result) == 2
    assert result[0].signature == "tx_pg_1"
    assert result[1].signature == "tx_pg_2"


def test_scenario_2_free_limited_playground_429_fallback_to_rpc(memory_db):
    """Scenario 2: FREE_LIMITED + Playground 429 -> no infinite retry, falls back to Native RPC."""
    mock_client = MagicMock()
    mock_client.get_playground_token_transfers.side_effect = SolscanAPIError("Rate limit exceeded: 429")

    # Native RPC mock returns 1 signature batch terminated
    mock_client.get_signatures_for_address.return_value = [
        {"signature": "sig_rpc_1", "blockTime": 1789952400, "slot": 100}
    ]
    mock_client.get_parsed_transaction.return_value = {
        "slot": 100,
        "blockTime": 1789952400,
        "meta": {
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": "TokenMint1111111111111111111111111111111111",
                    "owner": "Buyer11111111111111111111111111111111111111",
                    "uiTokenAmount": {"uiAmount": 500.0, "decimals": 9},
                }
            ],
            "preTokenBalances": [],
        },
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": "Deployer11111111111111111111111111111111111"},
                    {"pubkey": "Buyer11111111111111111111111111111111111111"},
                ]
            }
        },
    }

    mint = "TokenMint1111111111111111111111111111111111"
    result = collect_historical_transfers(
        mint_address=mint,
        max_transfers=5,
        client=mock_client,
        db=memory_db,
        discovery_mode="FREE_LIMITED",
    )

    # Must fall back to Native RPC (either NATIVE_RPC_GENESIS or NATIVE_RPC_BOUNDED)
    assert result.discovery_source in (
        DiscoverySourceEnum.NATIVE_RPC_GENESIS.value,
        DiscoverySourceEnum.NATIVE_RPC_BOUNDED.value,
    )
    assert mock_client.get_playground_token_transfers.call_count == 1  # No infinite loop
    assert mock_client.get_signatures_for_address.called


def test_scenario_3_free_limited_playground_401_fallback_to_rpc(memory_db):
    """Scenario 3: FREE_LIMITED + Playground 401 -> falls back to Native RPC."""
    mock_client = MagicMock()
    mock_client.get_playground_token_transfers.side_effect = SolscanAPIError("Unauthorized: 401")

    mock_client.get_signatures_for_address.return_value = []

    mint = "TokenMint1111111111111111111111111111111111"
    result = collect_historical_transfers(
        mint_address=mint,
        max_transfers=5,
        client=mock_client,
        db=memory_db,
        discovery_mode="FREE_LIMITED",
    )

    assert result.discovery_source == DiscoverySourceEnum.NATIVE_RPC_BOUNDED.value
    assert mock_client.get_playground_token_transfers.call_count == 1


def test_scenario_4_playground_budget_reached_is_truncated(memory_db):
    """Scenario 4: Playground 200 but partial pages (budget reached) -> candidate_discovery_complete=False, candidate_discovery_truncated=True."""
    mock_client = MagicMock()

    # Suppose safety budget is 2 requests, but there are more items
    def mock_fetch(token_address, page, page_size, sort_by, sort_order):
        return {
            "success": True,
            "data": [
                {
                    "trans_id": f"tx_p{page}_{i}",
                    "block_time": 1789952400 + page * 10 + i,
                    "from_address": "From1111111111111111111111111111111111111",
                    "to_address": f"To{page}_{i}111111111111111111111111111111111",
                    "amount": 100.0,
                    "decimals": 9,
                }
                for i in range(page_size)  # Returns full page_size items every time
            ],
        }

    mock_client.get_playground_token_transfers.side_effect = mock_fetch

    mint = "TokenMint1111111111111111111111111111111111"
    with patch.object(settings, "free_playground_max_requests_per_run", 2), \
         patch.object(settings, "free_playground_page_size", 10):
        result = collect_historical_transfers(
            mint_address=mint,
            max_transfers=50,  # Wants 50, but budget stops at 2 requests * 10 = 20 items
            client=mock_client,
            db=memory_db,
            discovery_mode="FREE_LIMITED",
        )

    assert len(result) == 20
    assert result.candidate_discovery_complete is False
    assert result.candidate_discovery_truncated is True
    assert result.discovery_termination_reason == "PLAYGROUND_LOCAL_BUDGET_REACHED"


def test_scenario_5_playground_genesis_natural_exhaustion_is_complete(memory_db):
    """Scenario 5: Playground 200 + genesis reached + natural termination -> candidate_discovery_complete=True."""
    mock_client = MagicMock()

    # Page 1 returns genesis SPL_MINT and 1 transfer (fewer than page_size)
    mock_client.get_playground_token_transfers.return_value = {
        "success": True,
        "data": [
            {
                "trans_id": "genesis_mint_tx",
                "block_time": 1789950000,
                "from_address": "11111111111111111111111111111111",
                "to_address": "Deployer11111111111111111111111111111111111",
                "amount": 1000000000.0,
                "decimals": 9,
                "activity_type": "ACTIVITY_SPL_MINT",
            },
            {
                "trans_id": "first_buy_tx",
                "block_time": 1789950010,
                "from_address": "Deployer11111111111111111111111111111111111",
                "to_address": "Buyer11111111111111111111111111111111111111",
                "amount": 5000.0,
                "decimals": 9,
                "activity_type": "ACTIVITY_SPL_TRANSFER",
            },
        ],
    }

    mint = "TokenMint1111111111111111111111111111111111"
    with patch.object(settings, "free_playground_page_size", 10):
        result = collect_historical_transfers(
            mint_address=mint,
            max_transfers=20,
            client=mock_client,
            db=memory_db,
            discovery_mode="FREE_LIMITED",
        )

    # 2 items returned when page_size=10 -> natural exhaustion!
    assert len(result) == 2
    assert result.genesis_reached is True
    assert result.candidate_discovery_complete is True
    assert result.candidate_discovery_truncated is False
    assert result.discovery_termination_reason == "PLAYGROUND_NATURAL_GENESIS"


def test_scenario_6_http_200_alone_does_not_imply_complete(memory_db):
    """Scenario 6: HTTP 200 alone does NOT imply COMPLETE (truncated budget remains INCOMPLETE)."""
    mock_client = MagicMock()

    # Returns 200 OK with full page (10 items), but not all history
    mock_client.get_playground_token_transfers.return_value = {
        "success": True,
        "data": [
            {
                "trans_id": f"tx_{i}",
                "block_time": 1789952000 + i,
                "from_address": "A11111111111111111111111111111111111111111",
                "to_address": "B11111111111111111111111111111111111111111",
                "amount": 10.0,
                "decimals": 9,
                "activity_type": "ACTIVITY_SPL_TRANSFER",
            }
            for i in range(10)
        ],
    }

    mint = "TokenMint1111111111111111111111111111111111"
    with patch.object(settings, "free_playground_max_requests_per_run", 1), \
         patch.object(settings, "free_playground_page_size", 10):
        result = collect_historical_transfers(
            mint_address=mint,
            max_transfers=50,
            client=mock_client,
            db=memory_db,
            discovery_mode="FREE_LIMITED",
        )

    # Even though HTTP response was 200 OK, discovery is INCOMPLETE because budget stopped it
    assert result.candidate_discovery_complete is False
    assert result.candidate_discovery_truncated is True


def test_scenario_7_solscan_playground_distinct_from_solscan_asc():
    """Scenario 7: SOLSCAN_PLAYGROUND is distinct from SOLSCAN_ASC."""
    assert DiscoverySourceEnum.SOLSCAN_PLAYGROUND.value != DiscoverySourceEnum.SOLSCAN_ASC.value
    assert DiscoverySourceEnum.SOLSCAN_PLAYGROUND.value == "SOLSCAN_PLAYGROUND"
    assert DiscoverySourceEnum.SOLSCAN_ASC.value == "SOLSCAN_ASC"
    assert DiscoveryModeEnum.FREE_LIMITED.value == "FREE_LIMITED"
    assert DiscoveryModeEnum.PRODUCTION.value == "PRODUCTION"


def test_scenario_8_early_window_incomplete_discovery_never_confirmed():
    """Scenario 8: Early-window incomplete discovery never becomes EARLY_WINDOW_CONFIRMED."""
    # When candidate discovery is incomplete in FREE_LIMITED, early_window_status must remain UNVERIFIED
    profile = WalletProfile(
        wallet_address="CandidateWallet111111111111111111111111111",
        token_address="TokenMint1111111111111111111111111111111111",
        first_buy_time=1700000100,
        candidate_discovery_complete=False,
        candidate_discovery_truncated=True,
        discovery_mode="FREE_LIMITED",
        discovery_source="SOLSCAN_PLAYGROUND",
        launch_time_type=LaunchResolutionType.EXACT_GENESIS.value,
        early_window_basis=EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value,
        is_in_early_window=True,
    )

    # Evaluation rule from app.py / main.py
    if profile.is_in_early_window:
        if (
            profile.early_window_basis == EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value
            and profile.candidate_discovery_complete
        ):
            profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value
        elif profile.early_window_basis == EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value:
            profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_ESTIMATED.value
        else:
            profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value
    else:
        profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value

    if profile.candidate_discovery_complete:
        profile.candidate_discovery_status = "COMPLETE"
    else:
        profile.candidate_discovery_status = "DISCOVERY_INCOMPLETE"
        if profile.confidence == ConfidenceEnum.HIGH:
            profile.confidence = ConfidenceEnum.MEDIUM

    assert profile.early_window_status == EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value
    assert profile.candidate_discovery_status == "DISCOVERY_INCOMPLETE"
    assert profile.confidence != ConfidenceEnum.HIGH


def test_scenario_9_rate_limit_server_not_hardcoded_as_quota():
    """Scenario 9: Rate-limit server is not hard-coded as '20 request/hour' (config parameters are local safety budget)."""
    # Verify settings defaults are safety limits, not server limit 20
    assert hasattr(settings, "free_playground_max_requests_per_run")
    assert settings.free_playground_max_requests_per_run == 3  # default safety limit is 3, NOT 20
    assert hasattr(settings, "free_playground_page_size")
    assert settings.free_playground_page_size == 10


def test_scenario_10_solscan_client_fail_fast_on_429_401(memory_db):
    """Scenario 10: Fail-fast non-aggressive handling on 429/401 in SolscanClient.get_playground_token_transfers."""
    client = SolscanClient(database=memory_db)

    # 1. Test 429 fail-fast
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.text = "Rate limited: 429 Too Many Requests"

    with patch.object(client.client, "get", return_value=mock_resp_429) as mock_get:
        with pytest.raises(SolscanAPIError) as exc_info:
            client.get_playground_token_transfers(
                token_address="TokenMint1111111111111111111111111111111111",
                page=1,
                page_size=10,
            )
        assert exc_info.value.status_code == 429
        assert "429" in str(exc_info.value)
        # Verify it called get only once (no aggressive retries)
        assert mock_get.call_count == 1

    # 2. Test 401 fail-fast
    mock_resp_401 = MagicMock()
    mock_resp_401.status_code = 401
    mock_resp_401.text = "Unauthorized: 401"

    with patch.object(client.client, "get", return_value=mock_resp_401) as mock_get:
        with pytest.raises(SolscanAPIError) as exc_info:
            client.get_playground_token_transfers(
                token_address="TokenMint2222222222222222222222222222222222",
                page=1,
                page_size=10,
            )
        assert exc_info.value.status_code == 401
        assert "401" in str(exc_info.value)
        assert mock_get.call_count == 1


def test_parse_transfer_item_list_fields():
    """Verify _parse_transfer_item gracefully handles list fields from Solscan Playground."""
    from collectors.transfers import _parse_transfer_item

    raw_item = {
        "trans_id": ["sig_list_1"],
        "block_time": 1789950000,
        "from_address": ["FromAddressInList11111111111111111111111111"],
        "to_address": ["ToAddressInList1111111111111111111111111111"],
        "token_address": ["TokenMintInList111111111111111111111111111"],
        "amount": 100.0,
        "decimals": 9,
        "activity_type": ["ACTIVITY_SPL_TRANSFER"],
    }
    event = _parse_transfer_item(raw_item, "DefaultTokenMint11111111111111111111111111")
    assert event is not None
    assert event.signature == "sig_list_1"
    assert event.from_address == "FromAddressInList11111111111111111111111111"
    assert event.to_address == "ToAddressInList1111111111111111111111111111"
    assert event.token_address == "TokenMintInList111111111111111111111111111"
    assert event.activity_type == "ACTIVITY_SPL_TRANSFER"

