"""Unit tests for Phase 4: Wallet Profiler, Scoring Engine, CLI Runner & Reporting.

Covers:
1. Exit ratio calculation and division by zero protection.
2. Time delta formatting (+2m14s, +45s, +1h12m, N/A).
3. Scoring engine (Early entry tiers, Buy size percentile & <5 fallback, Accumulation, Retention).
4. Token holders collector mapping.
5. End-to-end mock pipeline execution with CSV and JSON file exports.
"""

from pathlib import Path
import sys
import pytest
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyzers.sell_analyzer import analyze_wallet_sells
from analyzers.wallet_analyzer import build_buyer_profile, format_time_delta
from collectors.holders import get_token_holders_data
from models.schemas import (
    ClassificationEnum,
    ConfidenceEnum,
    TransactionClassification,
    WalletProfile,
)
from scoring.scorer import (
    calculate_accumulation_score,
    calculate_buy_size_scores,
    calculate_early_entry_score,
    calculate_holding_score,
    score_and_rank_buyers,
)
from main import run_mock_simulation
from storage.database import Database
from api.solscan import SolscanClient


@pytest.fixture
def memory_db():
    """Fixture providing an in-memory SQLite database instance."""
    db = Database(":memory:")
    yield db
    db.close()


# =====================================================================
# 1. TIME DELTA FORMATTING TESTS
# =====================================================================

def test_format_time_delta():
    """Verify conversion of seconds into human-readable relative time strings."""
    assert format_time_delta(None) == "N/A"
    assert format_time_delta(45) == "+45s"
    assert format_time_delta(0) == "+0s"
    assert format_time_delta(134) == "+2m14s"
    assert format_time_delta(182) == "+3m02s"
    assert format_time_delta(4320) == "+1h12m"
    assert format_time_delta(-30) == "-30s"


# =====================================================================
# 2. EXIT RATIO & ZERO-DIVISION PROTECTION TESTS
# =====================================================================

def test_analyze_wallet_sells_zero_division():
    """Verify exit_ratio calculation handles zero or negative total_acquired without ZeroDivisionError."""
    # Sells occurred but total_acquired is zero
    sells = [
        TransactionClassification(
            signature="sig_sell",
            wallet="WalletA",
            token_address="MintA",
            block_time=1000,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            token_change=-500.0,
            sol_change=0.5,
        )
    ]

    sell_count, total_sold, first_sell, est_exit, ratio = analyze_wallet_sells(
        wallet_address="WalletA",
        tx_classifications=sells,
        total_acquired=0.0,  # Zero acquired!
    )

    assert sell_count == 1
    assert total_sold == 500.0
    assert ratio == 0.0  # Must fallback cleanly to 0.0 without crashing!

    # Normal case: acquired 1,000, sold 250 -> ratio = 0.25
    _, _, _, _, normal_ratio = analyze_wallet_sells(
        wallet_address="WalletA",
        tx_classifications=sells,
        total_acquired=2000.0,
    )
    assert normal_ratio == 0.25


# =====================================================================
# 3. SCORING ENGINE PILLARS & EDGE CASES
# =====================================================================

def test_early_entry_score_tiers():
    """Verify Early Entry Score tiers based on seconds elapsed."""
    assert calculate_early_entry_score(60) == 100.0   # <= 2m
    assert calculate_early_entry_score(120) == 100.0  # exactly 2m
    assert calculate_early_entry_score(180) == 90.0   # 2-5m
    assert calculate_early_entry_score(500) == 75.0   # 5-10m
    assert calculate_early_entry_score(900) == 55.0   # 10-20m
    assert calculate_early_entry_score(2400) == 30.0  # 20-60m
    assert calculate_early_entry_score(7200) == 10.0  # > 60m

    # Fallback for LOW confidence or None
    assert calculate_early_entry_score(None) == 50.0
    assert calculate_early_entry_score(60, launch_confidence="LOW") == 50.0


def test_accumulation_score():
    """Verify Accumulation Score based on verified buy counts."""
    assert calculate_accumulation_score(1) == 50.0
    assert calculate_accumulation_score(2) == 75.0
    assert calculate_accumulation_score(3) == 100.0
    assert calculate_accumulation_score(10) == 100.0


def test_holding_score():
    """Verify Holding/Exit Score based on retention = (1.0 - exit_ratio)."""
    assert calculate_holding_score(0.0) == 100.0   # 100% hold
    assert calculate_holding_score(0.29) == 71.0   # 71% hold
    assert calculate_holding_score(1.0) == 0.0     # 100% exit
    assert calculate_holding_score(1.5) == 0.0     # clamped to 0


def test_buy_size_scores_fallback_under_5_buyers():
    """Verify min-max scaling fallback when total buyers < 5."""
    profiles = [
        WalletProfile(wallet_address="W1", token_address="M", total_buy_amount=100.0),
        WalletProfile(wallet_address="W2", token_address="M", total_buy_amount=500.0),
        WalletProfile(wallet_address="W3", token_address="M", total_buy_amount=1000.0),
    ]

    scores = calculate_buy_size_scores(profiles)
    assert len(scores) == 3
    # Min amount (100) -> 20.0
    assert scores["W1"] == 20.0
    # Max amount (1000) -> 100.0
    assert scores["W3"] == 100.0
    # Mid amount (500) -> ~55.56
    assert 50.0 < scores["W2"] < 60.0


def test_buy_size_scores_percentile_for_5_plus_buyers():
    """Verify percentile ranking when total buyers >= 5."""
    profiles = [
        WalletProfile(wallet_address="W1", token_address="M", total_buy_amount=100.0),
        WalletProfile(wallet_address="W2", token_address="M", total_buy_amount=200.0),
        WalletProfile(wallet_address="W3", token_address="M", total_buy_amount=300.0),
        WalletProfile(wallet_address="W4", token_address="M", total_buy_amount=400.0),
        WalletProfile(wallet_address="W5", token_address="M", total_buy_amount=500.0),
    ]

    scores = calculate_buy_size_scores(profiles)
    assert len(scores) == 5
    # W5 is at 100th percentile (P99+) -> 100.0
    assert scores["W5"] == 100.0
    # W1 is at 20th percentile (<P50) -> 20.0
    assert scores["W1"] == 20.0


def test_score_and_rank_buyers():
    """Verify end-to-end scoring and ranking ordering."""
    p1 = WalletProfile(
        wallet_address="EarlyWhale",
        token_address="M",
        time_after_launch=60,  # +1m -> Early=100
        total_buy_amount=5000.0,
        buy_count=3,           # Acc=100
        exit_ratio=0.0,        # Hold=100
        first_buy_time=100,
    )
    p2 = WalletProfile(
        wallet_address="LateSeller",
        token_address="M",
        time_after_launch=5000, # >1h -> Early=10
        total_buy_amount=100.0,
        buy_count=1,            # Acc=50
        exit_ratio=1.0,         # Hold=0
        first_buy_time=5000,
    )

    ranked = score_and_rank_buyers([p2, p1])
    assert len(ranked) == 2
    assert ranked[0][0].wallet_address == "EarlyWhale"
    assert ranked[0][0].score >= 90.0
    assert ranked[1][0].wallet_address == "LateSeller"
    assert ranked[1][0].score <= 40.0


# =====================================================================
# 4. TOKEN HOLDERS COLLECTOR MAPPING TEST
# =====================================================================

def test_get_token_holders_data(memory_db):
    """Verify holders response mapping to wallet dictionary."""
    mock_client = MagicMock(spec=SolscanClient)
    mock_client.get_token_holders.return_value = {
        "success": True,
        "data": {
            "items": [
                {
                    "address": "HolderA1111111111111111111111111111111111",
                    "amount": 250000.0,
                    "rank": 1,
                    "percentage": 25.0,
                    "value_usd": 12500.0,
                },
                {
                    "address": "HolderB1111111111111111111111111111111111",
                    "amount": 100000.0,
                    "rank": 2,
                    "percentage": 10.0,
                    "value_usd": 5000.0,
                },
            ]
        },
    }

    res = get_token_holders_data(
        mint_address="TokenMint1111111111111111111111111111111111",
        client=mock_client,
        db=memory_db,
    )

    assert len(res) == 2
    assert "HolderA1111111111111111111111111111111111" in res
    holder_a = res["HolderA1111111111111111111111111111111111"]
    assert holder_a["amount"] == 250000.0
    assert holder_a["rank"] == 1
    assert holder_a["percentage"] == 25.0


# =====================================================================
# 5. END-TO-END PIPELINE SIMULATION & REPORT EXPORT TEST
# =====================================================================

def test_end_to_end_mock_simulation_and_exports(tmp_path):
    """Verify that running mock simulation executes the complete pipeline and generates output files."""
    out_dir = tmp_path / "test_output"

    run_mock_simulation(
        output_dir=out_dir,
        export_csv_flag=True,
        export_json_flag=True,
    )

    csv_file = out_dir / "buyers.csv"
    json_file = out_dir / "report.json"

    assert csv_file.exists()
    assert json_file.exists()

    # Verify CSV contents
    content = csv_file.read_text(encoding="utf-8")
    assert "Rank,Wallet Address" in content
    assert "7xK9uN8tQAbc11111111111111111111111111111111" in content

    # Verify JSON contents
    json_text = json_file.read_text(encoding="utf-8")
    assert '"symbol": "MOJO"' in json_text
    assert '"likely_buyers_count": 3' in json_text
