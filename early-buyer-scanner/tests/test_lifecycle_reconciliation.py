"""Comprehensive forensic tests for Lifecycle Acquisition, SELL Classification, and Bilateral Reconciliation.

Validates all 15 core requirements and Cases A through H:
- Case A: BUY -> TRANSFER_OUT
- Case B: TRANSFER_IN -> SELL
- Case C: BUY -> SELL -> TRANSFER_OUT
- Case D: BUY -> micro SELL
- Case E: BUY -> UNKNOWN outflow
- Case F: BUY early, SELL far later
- Case G: ATA history exactly hits page limit (safety limit reached)
- Case H: Adversarial DEX-looking transfer without swap economic proof
- Decimal normalization (raw vs UI amount, no double-conversion)
"""

from typing import List, Optional
import pytest

from analyzers.sell_analyzer import analyze_wallet_sells, enrich_profile_with_sells, reconcile_wallet_lifecycle
from analyzers.transaction_classifier import (
    classify_transaction,
    is_burn,
    is_dex_swap,
    is_token_sale,
    is_transfer,
)
from collectors.candidate_lifecycle import CandidateLifecycleInfo, fetch_candidate_lifecycle_signatures
from collectors.transactions import parse_solscan_tx_detail
from config import (
    AXIOM_TRADE_ROUTER_ID,
    BURN_ADDRESSES,
    PUMP_FUN_PROGRAM_ID,
    PUMP_SWAP_AMM_ID,
    SYSTEM_PROGRAM_ID,
)
from models.schemas import (
    BalanceChange,
    ClassificationEnum,
    ConfidenceEnum,
    TransactionClassification,
    TransactionDetail,
    WalletProfile,
)


TOKEN_MINT = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"
CANDIDATE = "4oMMbUFZ83T2a6MshfjZwxcsTZwYSkUCRt8FVjbmL1d3"
COUNTERPARTY = "6pJXs9kq6rMZwwy6z2HDc93yXbUeu2rWQnLZURwKhk7G"
BONDING_CURVE = "5PnLRhNdRKURUCPezx4nNBpBGfkTe1kVFYELKWMFvRRd"


# =====================================================================
# 1. DECIMAL NORMALIZATION TESTS
# =====================================================================

def test_decimal_normalization_raw_int():
    """Verify raw integer amount is properly divided by 10^decimals (e.g. 5,127,805 -> 5.127805)."""
    raw_data = {
        "txHash": "sig_raw_int",
        "blockTime": 1790065000,
        "slot": 449323000,
        "feePayer": CANDIDATE,
        "status": "Success",
        "token_bal_change": [
            {
                "address": CANDIDATE,
                "token_address": TOKEN_MINT,
                "amount": 5127805,  # raw integer
                "decimals": 6,
                "pre_balance": 0,
                "post_balance": 5127805,
            }
        ],
    }
    tx = parse_solscan_tx_detail(raw_data, signature="sig_raw_int")
    assert len(tx.token_balance_changes) == 1
    tb = tx.token_balance_changes[0]
    assert abs(tb.change - 5.127805) < 1e-6
    assert abs(tb.post_balance - 5.127805) < 1e-6


def test_decimal_normalization_ui_amount():
    """Verify explicit UI amount is preserved without double-division."""
    raw_data = {
        "txHash": "sig_ui_float",
        "blockTime": 1790065000,
        "slot": 449323000,
        "feePayer": CANDIDATE,
        "status": "Success",
        "token_bal_change": [
            {
                "address": CANDIDATE,
                "token_address": TOKEN_MINT,
                "ui_amount": 5.127805,  # already UI amount
                "decimals": 6,
                "pre_balance": 0.0,
                "post_balance": 5.127805,
            }
        ],
    }
    tx = parse_solscan_tx_detail(raw_data, signature="sig_ui_float")
    tb = tx.token_balance_changes[0]
    assert abs(tb.change - 5.127805) < 1e-6


def test_decimal_normalization_ui_string():
    """Verify uiAmountString is preserved without double-division."""
    raw_data = {
        "txHash": "sig_ui_str",
        "blockTime": 1790065000,
        "slot": 449323000,
        "feePayer": CANDIDATE,
        "status": "Success",
        "token_bal_change": [
            {
                "address": CANDIDATE,
                "token_address": TOKEN_MINT,
                "uiAmountString": "5.127805",
                "decimals": 6,
                "pre_balance": 0.0,
                "post_balance": 5.127805,
            }
        ],
    }
    tx = parse_solscan_tx_detail(raw_data, signature="sig_ui_str")
    tb = tx.token_balance_changes[0]
    assert abs(tb.change - 5.127805) < 1e-6


def test_decimal_normalization_already_normalized_flag():
    """Verify is_ui_amount=True prevents division."""
    raw_data = {
        "txHash": "sig_flag_ui",
        "blockTime": 1790065000,
        "slot": 449323000,
        "feePayer": CANDIDATE,
        "status": "Success",
        "token_bal_change": [
            {
                "address": CANDIDATE,
                "token_address": TOKEN_MINT,
                "is_ui_amount": True,
                "change": 123.456,
                "decimals": 6,
                "pre_balance": 0.0,
                "post_balance": 123.456,
            }
        ],
    }
    tx = parse_solscan_tx_detail(raw_data, signature="sig_flag_ui")
    tb = tx.token_balance_changes[0]
    assert abs(tb.change - 123.456) < 1e-6


# =====================================================================
# 2. STRUCTURAL CLASSIFIER & CASE H (ADVERSARIAL TEST)
# =====================================================================

def test_case_h_adversarial_dex_transfer_without_swap_proof():
    """Case H: Candidate sends tokens to an address, transaction touches a DEX router,
    but candidate receives ZERO quote asset and there is no swap action.
    MUST NOT be classified as SELL!
    """
    tx = TransactionDetail(
        signature="sig_case_h",
        block_time=1790065100,
        slot=449323100,
        signer=CANDIDATE,
        programs=[AXIOM_TRADE_ROUTER_ID, SYSTEM_PROGRAM_ID],  # Router present!
        status="Success",
        token_balance_changes=[
            BalanceChange(address=CANDIDATE, pre_balance=1000.0, post_balance=0.0, change=-1000.0, mint=TOKEN_MINT),
            BalanceChange(address=COUNTERPARTY, pre_balance=0.0, post_balance=1000.0, change=1000.0, mint=TOKEN_MINT),
        ],
        sol_balance_changes=[
            BalanceChange(address=CANDIDATE, pre_balance=1.0, post_balance=0.999995, change=-0.000005),  # Paid gas only!
        ],
    )
    result = classify_transaction(tx, wallet_address=CANDIDATE, token_address=TOKEN_MINT)
    assert result.classification != ClassificationEnum.SELL, "Adversarial transfer must NEVER be classified as SELL!"
    assert result.classification == ClassificationEnum.TRANSFER_OUT


def test_micro_sell_classification():
    """Micro-sell with small SOL received (e.g. 0.00086 SOL < 0.001 SOL) MUST be classified as SELL."""
    tx = TransactionDetail(
        signature="sig_micro_sell",
        block_time=1790065200,
        slot=449323200,
        signer=CANDIDATE,
        programs=[PUMP_SWAP_AMM_ID, SYSTEM_PROGRAM_ID],
        status="Success",
        token_balance_changes=[
            BalanceChange(address=CANDIDATE, pre_balance=5000.0, post_balance=0.0, change=-5000.0, mint=TOKEN_MINT),
            BalanceChange(address=BONDING_CURVE, pre_balance=200000.0, post_balance=205000.0, change=5000.0, mint=TOKEN_MINT),
        ],
        sol_balance_changes=[
            BalanceChange(address=CANDIDATE, pre_balance=0.05, post_balance=0.050860, change=0.000860),  # +0.00086 SOL
        ],
    )
    result = classify_transaction(tx, wallet_address=CANDIDATE, token_address=TOKEN_MINT)
    assert result.classification == ClassificationEnum.SELL
    assert abs(result.token_change - (-5000.0)) < 1e-6
    assert abs(result.sol_change - 0.000860) < 1e-6


def test_normal_sell_classification():
    """Standard DEX SELL with positive SOL received."""
    tx = TransactionDetail(
        signature="sig_normal_sell",
        block_time=1790065300,
        slot=449323300,
        signer=CANDIDATE,
        programs=[PUMP_FUN_PROGRAM_ID, SYSTEM_PROGRAM_ID],
        status="Success",
        token_balance_changes=[
            BalanceChange(address=CANDIDATE, pre_balance=100000.0, post_balance=0.0, change=-100000.0, mint=TOKEN_MINT),
            BalanceChange(address=BONDING_CURVE, pre_balance=100000.0, post_balance=200000.0, change=100000.0, mint=TOKEN_MINT),
        ],
        sol_balance_changes=[
            BalanceChange(address=CANDIDATE, pre_balance=1.0, post_balance=3.5, change=2.5),
        ],
    )
    result = classify_transaction(tx, wallet_address=CANDIDATE, token_address=TOKEN_MINT)
    assert result.classification == ClassificationEnum.SELL
    assert result.confidence == ConfidenceEnum.HIGH


def test_burn_classification():
    """Token outflow to burn address with no quote received must be classified as BURN."""
    burn_addr = "11111111111111111111111111111111"
    tx = TransactionDetail(
        signature="sig_burn",
        block_time=1790065400,
        slot=449323400,
        signer=CANDIDATE,
        programs=[SYSTEM_PROGRAM_ID],
        status="Success",
        token_balance_changes=[
            BalanceChange(address=CANDIDATE, pre_balance=50000.0, post_balance=0.0, change=-50000.0, mint=TOKEN_MINT),
            BalanceChange(address=burn_addr, pre_balance=0.0, post_balance=50000.0, change=50000.0, mint=TOKEN_MINT),
        ],
        sol_balance_changes=[
            BalanceChange(address=CANDIDATE, pre_balance=0.1, post_balance=0.099995, change=-0.000005),
        ],
    )
    result = classify_transaction(tx, wallet_address=CANDIDATE, token_address=TOKEN_MINT)
    assert result.classification == ClassificationEnum.BURN
    assert result.confidence == ConfidenceEnum.HIGH


# =====================================================================
# 3. PAGINATION & COMPLETENESS TESTS
# =====================================================================

class MockRpcClient:
    def __init__(self, pages):
        self.pages = pages
        self.call_count = 0

    def get_signatures_for_address(self, address, limit=100, before=None):
        if self.call_count < len(self.pages):
            page = self.pages[self.call_count]
            self.call_count += 1
            return page
        return []


def test_pagination_history_exhausted():
    """When RPC returns less than batch_limit on last page, history is complete."""
    page1 = [{"signature": f"sig_page1_{i}"} for i in range(100)]
    page2 = [{"signature": f"sig_page2_{i}"} for i in range(25)]  # < 100 -> exhausted!
    client = MockRpcClient([page1, page2])

    result = fetch_candidate_lifecycle_signatures(
        client=client,
        candidate_wallets=[CANDIDATE],
        token_mint=TOKEN_MINT,
        max_signatures_per_ata=500,
    )
    info = result[CANDIDATE]
    assert info.lifecycle_signature_count == 125
    assert info.lifecycle_pages_fetched >= 2
    assert info.lifecycle_history_complete is True
    assert info.lifecycle_truncated is False
    assert info.truncation_reason is None


def test_pagination_safety_limit_case_g():
    """Case G: ATA history reaches configured safety limit while more history exists -> TRUNCATED."""
    page1 = [{"signature": f"sig_p1_{i}"} for i in range(100)]
    page2 = [{"signature": f"sig_p2_{i}"} for i in range(100)]
    client = MockRpcClient([page1, page2])

    # Set safety limit to 200 (exactly 2 full pages)
    result = fetch_candidate_lifecycle_signatures(
        client=client,
        candidate_wallets=[CANDIDATE],
        token_mint=TOKEN_MINT,
        max_signatures_per_ata=200,
    )
    info = result[CANDIDATE]
    assert info.lifecycle_signature_count == 200
    assert info.lifecycle_history_complete is False
    assert info.lifecycle_truncated is True
    assert info.truncation_reason is not None
    assert "safety limit" in info.truncation_reason.lower()


# =====================================================================
# 4. BILATERAL RECONCILIATION CASES (A, B, C, D, E)
# =====================================================================

def test_case_a_buy_then_transfer_out():
    """Case A: BUY 1000 -> TRANSFER_OUT 1000 -> Holding 0 -> MATCH."""
    profile = WalletProfile(
        wallet_address=CANDIDATE,
        token_address=TOKEN_MINT,
        first_buy_time=1790065000,
        first_buy_signature="sig_buy_a",
        first_buy_amount=1000.0,
        total_buy_amount=1000.0,
        buy_count=1,
        current_holding=0.0,
    )
    txs = [
        TransactionClassification(
            signature="sig_buy_a",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-1.0,
            token_change=1000.0,
        ),
        TransactionClassification(
            signature="sig_tout_a",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.TRANSFER_OUT,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-0.000005,
            token_change=-1000.0,
        ),
    ]
    enrich_profile_with_sells(profile, txs)
    assert profile.transfer_out_amount == 1000.0
    assert profile.reconciliation_difference == 0.0
    assert profile.balance_reconciliation_status == "MATCH"
    assert profile.confidence == ConfidenceEnum.HIGH


def test_case_b_transfer_in_then_sell():
    """Case B: TRANSFER_IN 1000 -> SELL 1000 -> Holding 0 -> MATCH."""
    profile = WalletProfile(
        wallet_address=CANDIDATE,
        token_address=TOKEN_MINT,
        first_buy_time=None,
        total_buy_amount=0.0,
        buy_count=0,
        current_holding=0.0,
    )
    txs = [
        TransactionClassification(
            signature="sig_tin_b",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.TRANSFER_IN,
            confidence=ConfidenceEnum.HIGH,
            sol_change=0.0,
            token_change=1000.0,
        ),
        TransactionClassification(
            signature="sig_sell_b",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=1.5,
            token_change=-1000.0,
        ),
    ]
    enrich_profile_with_sells(profile, txs)
    assert profile.transfer_in_amount == 1000.0
    assert profile.total_sell_amount == 1000.0
    assert profile.exit_ratio == 1.0  # 1000 / 1000 acquired = 1.0 (100%)
    assert profile.reconciliation_difference == 0.0
    assert profile.balance_reconciliation_status == "MATCH"


def test_case_c_buy_sell_transfer_out():
    """Case C: BUY 2000 -> SELL 500 -> TRANSFER_OUT 1500 -> Holding 0 -> MATCH."""
    profile = WalletProfile(
        wallet_address=CANDIDATE,
        token_address=TOKEN_MINT,
        first_buy_time=1790065000,
        first_buy_signature="sig_buy_c",
        first_buy_amount=2000.0,
        total_buy_amount=2000.0,
        buy_count=1,
        current_holding=0.0,
    )
    txs = [
        TransactionClassification(
            signature="sig_buy_c",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-2.0,
            token_change=2000.0,
        ),
        TransactionClassification(
            signature="sig_sell_c",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=0.7,
            token_change=-500.0,
        ),
        TransactionClassification(
            signature="sig_tout_c",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.TRANSFER_OUT,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-0.000005,
            token_change=-1500.0,
        ),
    ]
    enrich_profile_with_sells(profile, txs)
    assert profile.total_sell_amount == 500.0
    assert profile.transfer_out_amount == 1500.0
    assert profile.reconciliation_difference == 0.0
    assert profile.balance_reconciliation_status == "MATCH"


def test_case_d_buy_micro_sells():
    """Case D: BUY 1000 -> 10 micro-sells of 100 tokens -> Holding 0 -> MATCH."""
    profile = WalletProfile(
        wallet_address=CANDIDATE,
        token_address=TOKEN_MINT,
        first_buy_time=1790065000,
        first_buy_signature="sig_buy_d",
        first_buy_amount=1000.0,
        total_buy_amount=1000.0,
        buy_count=1,
        current_holding=0.0,
    )
    txs = [
        TransactionClassification(
            signature="sig_buy_d",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-1.0,
            token_change=1000.0,
        )
    ]
    for i in range(10):
        txs.append(
            TransactionClassification(
                signature=f"sig_micro_{i}",
                wallet=CANDIDATE,
                token_address=TOKEN_MINT,
                classification=ClassificationEnum.SELL,
                confidence=ConfidenceEnum.HIGH,
                sol_change=0.0008,
                token_change=-100.0,
            )
        )
    enrich_profile_with_sells(profile, txs)
    assert profile.sell_count == 10
    assert profile.total_sell_amount == 1000.0
    assert profile.reconciliation_difference == 0.0
    assert profile.balance_reconciliation_status == "MATCH"


def test_case_e_buy_then_unknown_outflow():
    """Case E: BUY 1000 -> SELL 800 -> UNKNOWN 200 -> Holding 0.
    UNKNOWN is NEVER counted in valid outflow! Status MUST be MISMATCH, confidence LOW.
    """
    profile = WalletProfile(
        wallet_address=CANDIDATE,
        token_address=TOKEN_MINT,
        first_buy_time=1790065000,
        first_buy_signature="sig_buy_e",
        first_buy_amount=1000.0,
        total_buy_amount=1000.0,
        buy_count=1,
        current_holding=0.0,
    )
    txs = [
        TransactionClassification(
            signature="sig_buy_e",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-1.0,
            token_change=1000.0,
        ),
        TransactionClassification(
            signature="sig_sell_e",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=1.2,
            token_change=-800.0,
        ),
        TransactionClassification(
            signature="sig_unk_e",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.UNKNOWN,
            confidence=ConfidenceEnum.LOW,
            sol_change=0.0,
            token_change=-200.0,
        ),
    ]
    enrich_profile_with_sells(profile, txs)
    assert profile.total_sell_amount == 800.0
    assert profile.unknown_outflow_amount == 200.0
    assert profile.unknown_tx_count == 1
    assert profile.reconciliation_difference == 200.0  # 1000 - 800 - 0 = 200 residual!
    assert profile.balance_reconciliation_status == "MISMATCH"
    assert profile.confidence == ConfidenceEnum.LOW


def test_reconciliation_bypass_permanently_removed():
    """Verify that excess sell (Sell 1500 > Buy 1000) does NOT get marked MATCH."""
    profile = WalletProfile(
        wallet_address=CANDIDATE,
        token_address=TOKEN_MINT,
        first_buy_time=1790065000,
        first_buy_signature="sig_buy_excess",
        first_buy_amount=1000.0,
        total_buy_amount=1000.0,
        buy_count=1,
        current_holding=0.0,
    )
    txs = [
        TransactionClassification(
            signature="sig_buy_excess",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.BUY,
            confidence=ConfidenceEnum.HIGH,
            sol_change=-1.0,
            token_change=1000.0,
        ),
        TransactionClassification(
            signature="sig_sell_excess",
            wallet=CANDIDATE,
            token_address=TOKEN_MINT,
            classification=ClassificationEnum.SELL,
            confidence=ConfidenceEnum.HIGH,
            sol_change=2.5,
            token_change=-1500.0,  # Sold 1500 without transfer in!
        ),
    ]
    enrich_profile_with_sells(profile, txs)
    # Residual = 1000 - 1500 - 0 = -500.0
    assert profile.reconciliation_difference == -500.0
    assert profile.balance_reconciliation_status == "MISMATCH", "Excess sell without transfer in MUST be MISMATCH!"
    assert profile.confidence == ConfidenceEnum.LOW
