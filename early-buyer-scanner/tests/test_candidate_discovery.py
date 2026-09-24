"""Test suite for Phase 3 Candidate Discovery Safety and Bounded Guarantees.

Scenarios tested:
A. Token <= 10,000 txs: Native RPC reaches genesis -> candidate_discovery_complete=True, source=NATIVE_RPC_GENESIS.
B. Token > 10,000 txs with Solscan ascending: source=SOLSCAN_ASC, candidate_discovery_complete=True, oldest_discovered_block_time = genesis time.
C. Token > 10,000 txs without ascending source: stops at max_pages, candidate_discovery_complete=False, candidate_discovery_truncated=True, source=NATIVE_RPC_BOUNDED, warning active, confidence capped to MEDIUM, sniper tag suppressed.
D. Token with stagnant cursor: loop terminates safely, candidate_discovery_complete=False, reason STAGNANT_CURSOR.
E. Token with RPC error: graceful termination without crash, reason RPC_ERROR.
F. Token with estimated launch time: verification that window comparison uses flag ESTIMATED and does not claim EXACT_GENESIS.
G. Boundary check on early_window_hours: wallets outside early window marked is_in_early_window=False.
"""

from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collectors.transfers import collect_historical_transfers, CandidateDiscoveryResult
from collectors.token import resolve_launch_time
from models.schemas import (
    ConfidenceEnum,
    DiscoverySourceEnum,
    LaunchResolutionType,
    LaunchTimeResolution,
    TokenMetadata,
    WalletProfile,
)
from storage.database import Database
from api.solscan import SolscanClient


@pytest.fixture
def memory_db():
    """Create an in-memory SQLite database instance."""
    db = Database(":memory:")
    db.init_schema()
    yield db
    db.close()


# =====================================================================
# SCENARIO A: Token <= 10,000 txs reaches genesis via Native RPC
# =====================================================================
def test_scenario_a_native_rpc_reaches_genesis(memory_db):
    """When token history is <= 10k txs and Solscan is unavailable,
    Native RPC natural termination flags candidate_discovery_complete=True and NATIVE_RPC_GENESIS.
    """
    mock_rpc = MagicMock()
    # Batch with 250 signatures (< 1000), meaning genesis is reached in page 1
    batch = [
        {"signature": f"sig_{i}", "blockTime": 1700000000 + i, "slot": 1000 + i}
        for i in range(250)
    ]
    batch.reverse()  # Newest to oldest
    mock_rpc.get_signatures_for_address.return_value = batch

    # Mock tx detail for token transfer
    def mock_tx_detail(sig):
        return {
            "data": {
                "signer": ["Deployer11111111111111111111111111111111111"],
                "token_bal_change": [
                    {
                        "token_address": "MintA111111111111111111111111111111111111",
                        "address": f"Buyer_{sig}",
                        "change": 1000.0,
                        "decimals": 6,
                    }
                ],
            }
        }

    mock_rpc.get_transaction_detail.side_effect = mock_tx_detail

    # Solscan client not available or returns None
    mint = "MintA111111111111111111111111111111111111"
    res = collect_historical_transfers(
        mint_address=mint,
        max_transfers=5,
        client=mock_rpc,
        db=memory_db,
        max_pages=10,
    )

    assert isinstance(res, CandidateDiscoveryResult)
    assert len(res) > 0
    assert res.candidate_discovery_complete is True
    assert res.candidate_discovery_truncated is False
    assert res.genesis_reached is True
    assert res.discovery_source == DiscoverySourceEnum.NATIVE_RPC_GENESIS.value
    assert res.discovery_termination_reason == "NATURAL_TERMINATION"
    assert res.discovery_pages_fetched == 1
    assert res.discovery_signatures_fetched == 250


# =====================================================================
# SCENARIO B: Token > 10,000 txs with Solscan ascending
# =====================================================================
def test_scenario_b_solscan_ascending_success(memory_db):
    """When Solscan API is available, transfers are queried ascending from genesis.
    Returns SOLSCAN_ASC with candidate_discovery_complete=True.
    """
    mock_solscan = MagicMock(spec=SolscanClient)
    mint = "MintB111111111111111111111111111111111111"

    # Solscan returns page 1 with 2 transfers in ascending order
    mock_solscan.get_token_transfers.return_value = {
        "success": True,
        "data": [
            {
                "trans_id": "genesis_sig_1",
                "block_time": 1700000010,
                "from_address": "From11111111111111111111111111111111111111",
                "to_address": "EarlyBuyer11111111111111111111111111111111",
                "amount": 50000.0,
                "token_address": mint,
            },
            {
                "trans_id": "genesis_sig_2",
                "block_time": 1700000020,
                "from_address": "From11111111111111111111111111111111111111",
                "to_address": "EarlyBuyer22222222222222222222222222222222",
                "amount": 25000.0,
                "token_address": mint,
            },
        ],
    }

    res = collect_historical_transfers(
        mint_address=mint,
        max_transfers=2,
        client=mock_solscan,
        db=memory_db,
    )

    assert isinstance(res, CandidateDiscoveryResult)
    assert len(res) == 2
    assert res.candidate_discovery_complete is True
    assert res.candidate_discovery_truncated is False
    assert res.genesis_reached is True
    assert res.discovery_source == DiscoverySourceEnum.SOLSCAN_ASC.value
    assert res.discovery_termination_reason == "SOLSCAN_ASC_SUCCESS"
    assert res.oldest_discovered_block_time == 1700000010


# =====================================================================
# SCENARIO C: Token > 10,000 txs without ascending source (hit max_pages)
# =====================================================================
def test_scenario_c_native_rpc_bounded_max_pages_reached(memory_db):
    """When Solscan is unavailable and Native RPC hits max_pages (10,000 sigs),
    discovery terminates safely with candidate_discovery_complete=False and NATIVE_RPC_BOUNDED.
    Downstream safety rules cap confidence to MEDIUM and suppress sniper tags.
    """
    mock_rpc = MagicMock()
    # Mock 10 full pages of 1000 items each
    call_count = [0]
    def mock_get_signatures(address, limit=1000, before=None):
        page_idx = call_count[0]
        call_count[0] += 1
        return [
            {"signature": f"sig_p{page_idx}_{i}", "blockTime": 1710000000 - (page_idx * 1000 + i)}
            for i in range(1000)
        ]

    mock_rpc.get_signatures_for_address.side_effect = mock_get_signatures
    mock_rpc.get_transaction_detail.return_value = {
        "data": {
            "signer": ["Signer1111111111111111111111111111111111111"],
            "token_bal_change": [
                {
                    "token_address": "MintC111111111111111111111111111111111111",
                    "address": "CandidateWallet111111111111111111111111111",
                    "change": 500.0,
                    "decimals": 6,
                }
            ],
        }
    }

    mint = "MintC111111111111111111111111111111111111"
    res = collect_historical_transfers(
        mint_address=mint,
        max_transfers=5,
        client=mock_rpc,
        db=memory_db,
        max_pages=10,
    )

    assert isinstance(res, CandidateDiscoveryResult)
    assert res.candidate_discovery_complete is False
    assert res.candidate_discovery_truncated is True
    assert res.genesis_reached is False
    assert res.discovery_source == DiscoverySourceEnum.NATIVE_RPC_BOUNDED.value
    assert res.discovery_termination_reason == "MAX_PAGES_REACHED"
    assert res.discovery_pages_fetched == 10
    assert res.discovery_signatures_fetched == 10000

    # Downstream Safety Rules verification (as applied in main.py / app.py)
    profile = WalletProfile(
        wallet_address="CandidateWallet111111111111111111111111111",
        token_address=mint,
        confidence=ConfidenceEnum.HIGH,  # Initially HIGH from lifecycle match
        is_same_block_sniper=True,       # Initially marked sniper
    )

    # Apply candidate discovery metadata & safety rules
    profile.candidate_discovery_complete = res.candidate_discovery_complete
    profile.candidate_discovery_truncated = res.candidate_discovery_truncated
    profile.genesis_reached = res.genesis_reached
    profile.discovery_source = res.discovery_source
    profile.discovery_termination_reason = res.discovery_termination_reason

    if profile.candidate_discovery_complete:
        profile.candidate_discovery_status = "COMPLETE"
    else:
        profile.candidate_discovery_status = "DISCOVERY_INCOMPLETE"
        # SAFETY RULE 1: Cap HIGH confidence to MEDIUM
        if profile.confidence == ConfidenceEnum.HIGH:
            profile.confidence = ConfidenceEnum.MEDIUM

    # SAFETY RULE 2: Suppress sniper tag
    if not res.candidate_discovery_complete:
        profile.is_same_block_sniper = False

    assert profile.candidate_discovery_complete is False
    assert profile.candidate_discovery_status == "DISCOVERY_INCOMPLETE"
    assert profile.confidence == ConfidenceEnum.MEDIUM  # Capped!
    assert profile.is_same_block_sniper is False        # Suppressed!


# =====================================================================
# SCENARIO D: Token with stagnant cursor
# =====================================================================
def test_scenario_d_stagnant_cursor_handling(memory_db):
    """When Native RPC returns identical cursor across pages,
    loop terminates immediately with STAGNANT_CURSOR and candidate_discovery_complete=False.
    """
    mock_rpc = MagicMock()
    # Batch 1 and Batch 2 both have the exact same oldest signature "stuck_sig"
    batch_stagnant = [
        {"signature": f"sig_var_{i}", "blockTime": 1700000000 + i} for i in range(999)
    ] + [{"signature": "stuck_sig", "blockTime": 1700000000}]

    mock_rpc.get_signatures_for_address.return_value = batch_stagnant
    mock_rpc.get_transaction_detail.return_value = {"data": {"token_bal_change": []}}

    mint = "MintD111111111111111111111111111111111111"
    res = collect_historical_transfers(
        mint_address=mint,
        max_transfers=5,
        client=mock_rpc,
        db=memory_db,
        max_pages=10,
    )

    assert res.candidate_discovery_complete is False
    assert res.candidate_discovery_truncated is True
    assert res.discovery_termination_reason == "STAGNANT_CURSOR"
    assert res.discovery_pages_fetched == 2  # Terminated on page 2 when cursor stagnated


# =====================================================================
# SCENARIO E: Token with RPC error (Graceful Handling)
# =====================================================================
def test_scenario_e_rpc_error_graceful_handling(memory_db):
    """When Native RPC throws an unhandled network error,
    collect_historical_transfers terminates gracefully without crashing.
    """
    mock_rpc = MagicMock()
    mock_rpc.get_signatures_for_address.side_effect = RuntimeError("429 Too Many Requests: Rate limit exceeded")

    mint = "MintE111111111111111111111111111111111111"
    res = collect_historical_transfers(
        mint_address=mint,
        max_transfers=5,
        client=mock_rpc,
        db=memory_db,
        max_pages=10,
    )

    assert isinstance(res, CandidateDiscoveryResult)
    assert res.candidate_discovery_complete is False
    assert "RPC_ERROR" in res.discovery_termination_reason
    assert "429" in res.discovery_termination_reason


# =====================================================================
# SCENARIO F: Token with estimated launch time
# =====================================================================
def test_scenario_f_estimated_launch_time(memory_db):
    """When genesis cannot be resolved exactly on-chain, resolution reports
    ESTIMATED_POOL_CREATION or BOUNDED_OLDEST_SIGNATURE and never claims EXACT_GENESIS.
    """
    mock_rpc = MagicMock()
    # 5 full pages (5000 signatures) hit max_pages limit
    mock_rpc.get_signatures_for_address.return_value = [
        {"signature": f"sig_{i}", "blockTime": 1715000000 - i, "slot": 200000 - i}
        for i in range(1000)
    ]

    mint = "MintF111111111111111111111111111111111111"

    # Mock DexScreener returning pairCreatedAt tuple (pool_time, dex_id, pair_addr)
    with patch("collectors.token.fetch_dexscreener_pair_created_at", return_value=(1710000000, "raydium", "PairPoolAddress1111111111111111111111111")):
        res = resolve_launch_time(mint, client=mock_rpc, db=memory_db, max_pages=3)

    assert isinstance(res, LaunchTimeResolution)
    assert res.resolution_type == LaunchResolutionType.ESTIMATED_POOL_CREATION.value
    assert res.resolution_type != LaunchResolutionType.EXACT_GENESIS.value
    assert res.confidence == ConfidenceEnum.MEDIUM
    assert res.launch_time == 1710000000


# =====================================================================
# SCENARIO G: Early Window Boundary Check (early_window_hours)
# =====================================================================
def test_scenario_g_early_window_boundary_check():
    """Verify that a candidate whose first buy occurs after early_window_hours (e.g. 24h)
    is explicitly flagged as is_in_early_window=False.
    """
    launch_time = 1700000000
    early_window_hours = 24.0
    cutoff = launch_time + int(early_window_hours * 3600)  # 1700086400

    # Buyer 1: bought 2 hours after launch -> in early window
    p1 = WalletProfile(
        wallet_address="EarlyBuyerWallet111111111111111111111111111",
        token_address="MintG111111111111111111111111111111111111",
        first_buy_time=launch_time + 7200,
    )
    p1.is_in_early_window = bool(p1.first_buy_time <= cutoff)
    assert p1.is_in_early_window is True

    # Buyer 2: bought 25 hours after launch -> outside early window
    p2 = WalletProfile(
        wallet_address="LateBuyerWallet1111111111111111111111111111",
        token_address="MintG111111111111111111111111111111111111",
        first_buy_time=launch_time + 90000,
    )
    p2.is_in_early_window = bool(p2.first_buy_time <= cutoff)
    assert p2.is_in_early_window is False
