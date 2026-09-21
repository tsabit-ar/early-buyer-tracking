"""Unit tests for V2 — Wallet Intelligence & Sybil Detection.

Covers:
1. Tracing CEX funded fresh wallets (Inflow Extraction + CEX mapping).
2. True funder balance decrease extraction from genesis transactions.
3. Bounded depth limit (max 3 pages) & early termination on mature wallets (> 7 days).
4. Sybil cluster detection with threshold (> 0.05 SOL) & CEX clustering exemption.
5. SQLite schema migration and roundtrip persistence of funding & cluster fields.
"""

from pathlib import Path
import sys
from unittest.mock import MagicMock
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyzers.funding_analyzer import detect_funder_clusters, trace_wallet_funder
from config import KNOWN_CEX_WALLETS
from models.schemas import ConfidenceEnum, WalletProfile
from storage.database import Database


@pytest.fixture
def memory_db():
    """Provide an in-memory SQLite database instance."""
    db = Database(":memory:")
    yield db
    db.close()


def test_trace_wallet_funder_cex():
    """Verify tracing a fresh wallet funded by Binance CEX."""
    mock_client = MagicMock()
    wallet_addr = "FreshBuyer11111111111111111111111111111111"
    binance_addr = "5tzFkiKscMRHK5ZXkrZXZ1RChPTyVC5yFsNuPaSkWCjd"
    launch_time = 1789952445

    # 1. Page with single genesis transaction 30 minutes before launch
    mock_client.get_signatures_for_address.return_value = [
        {
            "signature": "sig_genesis_cex",
            "blockTime": launch_time - 1800,  # 30 min old (< 24h)
        }
    ]

    # 2. Parsed transaction detail with SOL balance changes
    mock_client.get_transaction_detail.return_value = {
        "data": {
            "signer": [binance_addr],
            "sol_bal_change": [
                {
                    "address": wallet_addr,
                    "pre_balance": 0.0,
                    "post_balance": 2.5,
                    "change": 2.5,
                },
                {
                    "address": binance_addr,
                    "pre_balance": 1000.0,
                    "post_balance": 997.5,
                    "change": -2.5,
                },
            ],
        }
    }

    funder_addr, funder_type, age_days, fund_amt, fund_sig = trace_wallet_funder(
        wallet_address=wallet_addr,
        rpc_client=mock_client,
        launch_time=launch_time,
    )

    assert funder_addr == binance_addr
    assert funder_type == "CEX"
    assert age_days is not None and age_days < 1.0  # Fresh (< 24h)
    assert fund_amt == 2.5
    assert fund_sig == "sig_genesis_cex"


def test_trace_wallet_funder_inflow_extraction():
    """Verify identification of the sender account whose SOL balance decreased."""
    mock_client = MagicMock()
    wallet_addr = "BuyerWallet1111111111111111111111111111111"
    eoa_funder = "FunderEOA1111111111111111111111111111111111"
    launch_time = 1789952445

    mock_client.get_signatures_for_address.return_value = [
        {
            "signature": "sig_genesis_eoa",
            "blockTime": launch_time - 7200,  # 2 hours old
        }
    ]

    mock_client.get_transaction_detail.return_value = {
        "data": {
            "signer": [eoa_funder],
            "sol_bal_change": [
                {
                    "address": "RandomThirdParty11111111111111111111111111",
                    "pre_balance": 0.1,
                    "post_balance": 0.1,
                    "change": 0.0,
                },
                {
                    "address": wallet_addr,
                    "pre_balance": 0.0,
                    "post_balance": 1.25,
                    "change": 1.25,
                },
                {
                    "address": eoa_funder,
                    "pre_balance": 10.0,
                    "post_balance": 8.74,
                    "change": -1.26,
                },
            ],
        }
    }

    funder_addr, funder_type, age_days, fund_amt, fund_sig = trace_wallet_funder(
        wallet_address=wallet_addr,
        rpc_client=mock_client,
        launch_time=launch_time,
    )

    assert funder_addr == eoa_funder
    assert funder_type == "EOA"
    assert fund_amt == 1.25
    assert age_days is not None and age_days < 1.0


def test_trace_wallet_funder_depth_limit_and_mature():
    """Verify early termination on mature wallets (> 7 days old) to protect RPC quota."""
    mock_client = MagicMock()
    wallet_addr = "MatureTrader1111111111111111111111111111111"
    launch_time = 1789952445

    # Page 1 contains a transaction from 20 days ago (> MATURE_WALLET_AGE_DAYS)
    old_tx_time = launch_time - (20 * 86400)
    mock_client.get_signatures_for_address.return_value = [
        {"signature": f"sig_rec_{i}", "blockTime": launch_time - 100} for i in range(999)
    ] + [{"signature": "sig_old", "blockTime": old_tx_time}]

    funder_addr, funder_type, age_days, fund_amt, fund_sig = trace_wallet_funder(
        wallet_address=wallet_addr,
        rpc_client=mock_client,
        launch_time=launch_time,
    )

    # Must stop immediately at page 1 without fetching subsequent pages
    assert mock_client.get_signatures_for_address.call_count == 1
    assert funder_type == "MATURE_WALLET"
    assert age_days is not None and age_days >= 7.0
    assert funder_addr is None
    assert fund_amt is None


def test_detect_funder_clusters_sybil_threshold():
    """Verify grouping wallets sharing the same EOA funder with > 0.05 SOL threshold and CEX exemption."""
    eoa_funder_1 = "SybilMaster1111111111111111111111111111111"
    eoa_funder_solo = "SoloFunder111111111111111111111111111111111"
    micro_funder = "MicroFunder11111111111111111111111111111111"
    cex_funder = "5tzFkiKscMRHK5ZXkrZXZ1RChPTyVC5yFsNuPaSkWCjd"  # Binance

    profiles = [
        # Cluster 1: 3 wallets funded by eoa_funder_1 (> 0.05 SOL)
        WalletProfile(wallet_address="W1", token_address="T", funder_address=eoa_funder_1, funder_type="EOA", funding_amount_sol=1.5),
        WalletProfile(wallet_address="W2", token_address="T", funder_address=eoa_funder_1, funder_type="EOA", funding_amount_sol=2.0),
        WalletProfile(wallet_address="W3", token_address="T", funder_address=eoa_funder_1, funder_type="EOA", funding_amount_sol=0.8),
        # Solo EOA funder (only 1 wallet -> NO cluster)
        WalletProfile(wallet_address="W4", token_address="T", funder_address=eoa_funder_solo, funder_type="EOA", funding_amount_sol=1.0),
        # CEX funded wallets (2 wallets -> MUST NOT be clustered!)
        WalletProfile(wallet_address="W5", token_address="T", funder_address=cex_funder, funder_type="CEX", funding_amount_sol=5.0),
        WalletProfile(wallet_address="W6", token_address="T", funder_address=cex_funder, funder_type="CEX", funding_amount_sol=10.0),
        # Micro funders (2 wallets, but < 0.05 SOL threshold -> NO cluster)
        WalletProfile(wallet_address="W7", token_address="T", funder_address=micro_funder, funder_type="EOA", funding_amount_sol=0.01),
        WalletProfile(wallet_address="W8", token_address="T", funder_address=micro_funder, funder_type="EOA", funding_amount_sol=0.02),
    ]

    clustered = detect_funder_clusters(profiles, min_funding_amount=0.05)

    assert clustered[0].cluster_id == "CLUSTER_1"
    assert clustered[1].cluster_id == "CLUSTER_1"
    assert clustered[2].cluster_id == "CLUSTER_1"

    # W4 solo: no cluster
    assert clustered[3].cluster_id is None

    # W5 & W6 CEX: never clustered
    assert clustered[4].cluster_id is None
    assert clustered[5].cluster_id is None

    # W7 & W8 Micro: below threshold
    assert clustered[6].cluster_id is None
    assert clustered[7].cluster_id is None


def test_wallet_profile_db_roundtrip_funding(memory_db):
    """Verify SQLite persistence and roundtrip reading of new funding and clustering fields."""
    token = "TestToken1111111111111111111111111111111111"
    wallet = "TestBuyer111111111111111111111111111111111"
    funder = "Funder111111111111111111111111111111111111"

    profile = WalletProfile(
        wallet_address=wallet,
        token_address=token,
        first_buy_time=1000,
        first_buy_slot=448900000,
        first_buy_signature="sig_buy",
        first_buy_amount=5000.0,
        total_buy_amount=5000.0,
        buy_count=1,
        score=85.0,
        confidence=ConfidenceEnum.HIGH,
        wallet_age_days=0.125,
        is_fresh_wallet=True,
        funder_address=funder,
        funder_type="EOA",
        funding_amount_sol=1.5,
        funding_signature="sig_fund",
        cluster_id="CLUSTER_1",
    )

    memory_db.save_wallet_profile(profile)

    profiles = memory_db.get_wallet_profiles(token)
    assert len(profiles) == 1
    p = profiles[0]

    assert p.wallet_address == wallet
    assert p.wallet_age_days == 0.125
    assert p.is_fresh_wallet is True
    assert p.funder_address == funder
    assert p.funder_type == "EOA"
    assert p.funding_amount_sol == 1.5
    assert p.funding_signature == "sig_fund"
    assert p.cluster_id == "CLUSTER_1"
