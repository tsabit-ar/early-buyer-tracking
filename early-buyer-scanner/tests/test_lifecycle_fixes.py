"""Unit and architecture regression tests for Lifecycle Data Acquisition (BUG 1) and Decimal Normalization (BUG 2).

Verifies:
1. UI amounts (ui_amount, uiAmount, uiAmountString, is_ui_amount=True) are not divided again.
2. Raw integer amounts are deterministically normalized based on token decimals without heuristics.
3. Neither double conversion nor under conversion occurs.
4. Architectural simulation: SELL occurs far outside the initial transfer window (e.g. transfer #500+ with max_transfers=50),
   and is successfully captured via candidate ATA lifecycle history.
5. Lifecycle invariants: reconciliation matching and incomplete history tagging.
6. Target wallet lifecycle regressions (2iDAb, BLm7, DN19).
"""

from pathlib import Path
import sys
from unittest.mock import MagicMock
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyzers.sell_analyzer import enrich_profile_with_sells, reconcile_wallet_lifecycle
from analyzers.transaction_classifier import classify_transaction
from collectors.candidate_lifecycle import (
    derive_candidate_atas,
    fetch_candidate_lifecycle_signatures,
)
from collectors.transactions import parse_solscan_tx_detail
from models.schemas import (
    ClassificationEnum,
    ConfidenceEnum,
    TransactionClassification,
    TransactionDetail,
    WalletProfile,
)
from storage.database import Database


# =====================================================================
# 1. DETERMINISTIC DECIMAL NORMALIZATION TESTS
# =====================================================================

def test_decimal_normalization_ui_amount_not_divided():
    """1. UI amount must NOT be divided again.
    Input: ui_amount = 5.127805, decimals = 6 -> Expected: 5.127805 (NOT 0.000005127805).
    """
    raw_payload = {
        "success": True,
        "data": {
            "tx_hash": "sig_ui_test",
            "token_bal_change": [
                {
                    "address": "WalletA",
                    "token_address": "TokenMeme1",
                    "pre_balance": 10.0,
                    "post_balance": 15.127805,
                    "ui_amount": 5.127805,
                    "decimals": 6,
                }
            ],
        },
    }
    parsed = parse_solscan_tx_detail(raw_payload, "sig_ui_test")
    assert len(parsed.token_balance_changes) == 1
    tc = parsed.token_balance_changes[0]
    assert tc.change == 5.127805
    assert tc.change != pytest.approx(0.000005127805)


def test_decimal_normalization_raw_amount_converted():
    """2. Raw amount must be converted using token decimals.
    Input: raw amount = 5127805, decimals = 6 -> Expected: 5.127805.
    """
    raw_payload = {
        "success": True,
        "data": {
            "tx_hash": "sig_raw_test",
            "token_bal_change": [
                {
                    "address": "WalletA",
                    "token_address": "TokenMeme1",
                    "amount": 5127805,
                    "pre_balance": "0",
                    "post_balance": "5127805",
                    "decimals": 6,
                }
            ],
        },
    }
    parsed = parse_solscan_tx_detail(raw_payload, "sig_raw_test")
    assert len(parsed.token_balance_changes) == 1
    tc = parsed.token_balance_changes[0]
    assert tc.change == pytest.approx(5.127805)


def test_decimal_normalization_ui_amount_string_no_double_conversion():
    """3. uiAmountString must not be converted twice."""
    raw_payload = {
        "success": True,
        "data": {
            "tx_hash": "sig_ui_str_test",
            "token_bal_change": [
                {
                    "address": "WalletB",
                    "token_address": "TokenMeme1",
                    "uiAmountString": "60.438219",
                    "pre_balance": 0.0,
                    "post_balance": 60.438219,
                    "decimals": 6,
                }
            ],
        },
    }
    parsed = parse_solscan_tx_detail(raw_payload, "sig_ui_str_test")
    assert len(parsed.token_balance_changes) == 1
    assert parsed.token_balance_changes[0].change == pytest.approx(60.438219)


def test_decimal_normalization_is_ui_amount_flag_prevents_division():
    """4. is_ui_amount=True must explicitly prevent division."""
    raw_payload = {
        "success": True,
        "data": {
            "tx_hash": "sig_flag_test",
            "token_bal_change": [
                {
                    "address": "WalletC",
                    "token_address": "TokenMeme1",
                    "change": 4012469.902709,
                    "pre_balance": 0.0,
                    "post_balance": 4012469.902709,
                    "decimals": 6,
                    "is_ui_amount": True,
                }
            ],
        },
    }
    parsed = parse_solscan_tx_detail(raw_payload, "sig_flag_test")
    assert len(parsed.token_balance_changes) == 1
    assert parsed.token_balance_changes[0].change == pytest.approx(4012469.902709)


def test_decimal_normalization_large_raw_amount_converted():
    """Verify raw integer amounts for DN19 scale (4012469902709 base units -> 4012469.902709)."""
    raw_payload = {
        "success": True,
        "data": {
            "tx_hash": "sig_dn19_raw",
            "token_bal_change": [
                {
                    "address": "WalletDN19",
                    "token_address": "TokenMeme1",
                    "raw_amount": 4012469902709,
                    "pre_balance": 0,
                    "post_balance": 4012469902709,
                    "decimals": 6,
                }
            ],
        },
    }
    parsed = parse_solscan_tx_detail(raw_payload, "sig_dn19_raw")
    assert parsed.token_balance_changes[0].change == pytest.approx(4012469.902709)


# =====================================================================
# 2. ARCHITECTURAL SIMULATION TEST: SELL OUTSIDE INITIAL WINDOW (BUG 1)
# =====================================================================

def test_sell_outside_initial_window_captured_via_ata():
    """6. Architectural regression test:
    Simulate BUY in transfer #10 and SELL in transfer #500+.
    With max_transfers=50, the global scan never sees the SELL.
    Verify that candidate ATA lifecycle history queries getSignaturesForAddress
    and discovers the SELL transaction.
    """
    candidate_wallet = "2iDAbmU7i5bUiCkLr7GKHJrwbS2FANQbZJ81fXTbwjap"
    token_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"

    # Derive candidate ATAs
    candidate_atas = derive_candidate_atas(candidate_wallet, token_mint)
    assert len(candidate_atas) >= 1
    target_ata = candidate_atas[0]

    # Mock client where global transfer window only has BUY, but ATA has BUY and SELL #500
    mock_client = MagicMock()
    buy_sig = "sig_transfer_010_buy"
    sell_sig = "sig_transfer_550_sell_late"

    # When querying candidate ATA, it returns both buy and late sell
    mock_client.get_signatures_for_address.return_value = [
        {"signature": sell_sig, "slot": 449325000, "blockTime": 1790066000},
        {"signature": buy_sig, "slot": 449323010, "blockTime": 1790065200},
    ]

    lifecycle_map = fetch_candidate_lifecycle_signatures(
        client=mock_client,
        candidate_wallets=[candidate_wallet],
        token_mint=token_mint,
        max_signatures_per_ata=100,
    )

    assert candidate_wallet in lifecycle_map
    info = lifecycle_map[candidate_wallet]
    assert info.lifecycle_history_complete is True
    assert buy_sig in info.signatures
    assert sell_sig in info.signatures
    assert len(info.signatures) == 2


def test_ata_pagination_does_not_silently_truncate():
    """Verify that fetch_candidate_lifecycle_signatures paginates through multiple pages
    and flags incomplete history if the safety limit is reached.
    """
    candidate_wallet = "BLm7PT4iUYgRFum1LkrhJofN3tkHzjjDsYY6QjbwRBaS"
    token_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"

    mock_client = MagicMock()
    # Return 100 signatures on first page, 50 on second page (total 150)
    page1 = [{"signature": f"sig_page1_{i}"} for i in range(100)]
    page2 = [{"signature": f"sig_page2_{i}"} for i in range(50)]

    mock_client.get_signatures_for_address.side_effect = [page1, page2, []]

    # Safety limit set to 200 -> should fetch all 150 and remain complete
    lifecycle_map = fetch_candidate_lifecycle_signatures(
        client=mock_client,
        candidate_wallets=[candidate_wallet],
        token_mint=token_mint,
        max_signatures_per_ata=200,
    )
    info = lifecycle_map[candidate_wallet]
    assert info.lifecycle_history_complete is True
    assert len(info.signatures) == 150

    # Test safety limit truncation: safety limit = 50 -> should truncate and set complete=False
    mock_client.get_signatures_for_address.side_effect = [page1[:50]]
    truncated_map = fetch_candidate_lifecycle_signatures(
        client=mock_client,
        candidate_wallets=[candidate_wallet],
        token_mint=token_mint,
        max_signatures_per_ata=50,
    )
    trunc_info = truncated_map[candidate_wallet]
    assert trunc_info.lifecycle_history_complete is False


# =====================================================================
# 3. LIFECYCLE INVARIANTS & RECONCILIATION TESTS (PART 5)
# =====================================================================

def test_lifecycle_invariant_match():
    """Verify balance reconciliation MATCH when all tokens bought are sold and holding is 0."""
    wallet = "WalletMatch11111111111111111111111111111111"
    token = "Token11111111111111111111111111111111111111"

    txs = [
        TransactionClassification(
            signature="sig_buy",
            wallet=wallet,
            token_address=token,
            block_time=100,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-1.0,
            token_change=1000.0,
        ),
        TransactionClassification(
            signature="sig_sell",
            wallet=wallet,
            token_address=token,
            block_time=200,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=1.2,
            token_change=-1000.0,
        ),
    ]

    profile = WalletProfile(
        wallet_address=wallet,
        token_address=token,
        first_buy_time=100,
        first_buy_amount=1000.0,
        total_buy_amount=1000.0,
        buy_count=1,
        current_holding=0.0,
        lifecycle_history_complete=True,
    )

    enrich_profile_with_sells(profile, txs)
    assert profile.sell_count == 1
    assert profile.total_sell_amount == 1000.0
    assert profile.exit_ratio == 1.0
    assert profile.balance_reconciliation_status == "MATCH"


def test_lifecycle_invariant_mismatch_and_incomplete():
    """Verify that if holding is 0 but sells < buy:
    - If history complete: MISMATCH.
    - If history incomplete: INCOMPLETE and confidence downgraded from HIGH.
    """
    wallet = "WalletMismatch11111111111111111111111111111"
    token = "Token11111111111111111111111111111111111111"

    txs = [
        TransactionClassification(
            signature="sig_buy",
            wallet=wallet,
            token_address=token,
            block_time=100,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-1.0,
            token_change=1000.0,
        ),
        # Only sold 400 out of 1000, but holding is 0!
        TransactionClassification(
            signature="sig_sell_partial",
            wallet=wallet,
            token_address=token,
            block_time=200,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=0.5,
            token_change=-400.0,
        ),
    ]

    # Case A: Complete history but missing outflow -> MISMATCH
    profile_complete = WalletProfile(
        wallet_address=wallet,
        token_address=token,
        first_buy_time=100,
        first_buy_amount=1000.0,
        total_buy_amount=1000.0,
        buy_count=1,
        current_holding=0.0,
        lifecycle_history_complete=True,
    )
    enrich_profile_with_sells(profile_complete, txs)
    assert profile_complete.balance_reconciliation_status == "MISMATCH"

    # Case B: Incomplete history -> INCOMPLETE, and confidence downgraded from HIGH
    profile_incomplete = WalletProfile(
        wallet_address=wallet,
        token_address=token,
        first_buy_time=100,
        first_buy_amount=1000.0,
        total_buy_amount=1000.0,
        buy_count=1,
        current_holding=0.0,
        confidence=ConfidenceEnum.HIGH,
        lifecycle_history_complete=False,
    )
    enrich_profile_with_sells(profile_incomplete, txs)
    assert profile_incomplete.balance_reconciliation_status == "INCOMPLETE"
    assert profile_incomplete.confidence != ConfidenceEnum.HIGH
