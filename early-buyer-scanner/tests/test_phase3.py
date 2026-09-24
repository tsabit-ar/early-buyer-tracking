"""Unit tests for Phase 3: Transaction Detail Collector & Classification Engine.

Covers:
1. Real token ACAT BUY swap with signature 5ksNkCfpLgPheeeKrJ5DdY5zEXurFkibqT7dQSmEDxnrKqBYdBDbFuhTVWpvNtL1dfVo3T9uJRhnQZBFtxw4bqWq.
2. Raydium WSOL BUY swap (quote asset is wrapped SOL).
3. Standard TRANSFER with 0.000005 SOL gas fee (assert not classified as BUY).
4. Airdrop/mint DISTRIBUTION with zero quote asset spent.
5. SQLite-first caching in get_transaction_details_batch.
6. SELL and failed transaction classification.
"""

from pathlib import Path
import sys
import pytest
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    PUMP_FUN_PROGRAM_ID,
    RAYDIUM_AMM_V4_ID,
    RAYDIUM_CPMM_ID,
    SYSTEM_PROGRAM_ID,
    WSOL_MINT,
)
from collectors.transactions import get_transaction_details_batch, parse_solscan_tx_detail
from analyzers.transaction_classifier import (
    FEE_THRESHOLD_SOL,
    classify_transaction,
)
from models.schemas import (
    BalanceChange,
    ClassificationEnum,
    ConfidenceEnum,
    TransactionDetail,
)
from storage.database import Database
from api.solscan import SolscanClient


@pytest.fixture
def memory_db():
    """Fixture providing an in-memory SQLite database instance."""
    db = Database(":memory:")
    yield db
    db.close()


# =====================================================================
# 1. ACAT BUY SWAP TEST
# =====================================================================

def test_acat_buy_swap():
    """Mock test BUY swap modeling ACAT token purchase with signature:
    5ksNkCfpLgPheeeKrJ5DdY5zEXurFkibqT7dQSmEDxnrKqBYdBDbFuhTVWpvNtL1dfVo3T9uJRhnQZBFtxw4bqWq
    
    Verifies SOL outflow > 0.003, ACAT token inflow, and DEX program detection.
    """
    sig = "5ksNkCfpLgPheeeKrJ5DdY5zEXurFkibqT7dQSmEDxnrKqBYdBDbFuhTVWpvNtL1dfVo3T9uJRhnQZBFtxw4bqWq"
    acat_mint = "ACATmint111111111111111111111111111111111111"
    buyer_wallet = "7xK9uN8tQAbc11111111111111111111111111111111"

    tx = TransactionDetail(
        signature=sig,
        block_time=1716000100,
        signer=buyer_wallet,
        sol_balance_changes=[
            BalanceChange(
                address=buyer_wallet,
                pre_balance=10.0,
                post_balance=8.5,
                change=-1.5,  # SOL spent: 1.5 SOL > 0.003
                decimals=9,
            )
        ],
        token_balance_changes=[
            BalanceChange(
                address=buyer_wallet,
                pre_balance=0.0,
                post_balance=4500000.0,
                change=4500000.0,  # ACAT tokens received
                mint=acat_mint,
                decimals=6,
            )
        ],
        programs=[RAYDIUM_AMM_V4_ID, SYSTEM_PROGRAM_ID],
        status="Success",
        priority_fee=0.00005,
    )

    result = classify_transaction(tx, wallet_address=buyer_wallet, token_address=acat_mint)

    assert result.signature == sig
    assert result.classification == ClassificationEnum.BUY
    assert result.confidence == ConfidenceEnum.HIGH
    assert result.sol_change == -1.5
    assert result.token_change == 4500000.0
    assert RAYDIUM_AMM_V4_ID in result.programs


# =====================================================================
# 2. RAYDIUM WSOL BUY TEST
# =====================================================================

def test_raydium_wsol_buy():
    """Verify BUY classification when paying with Wrapped SOL (WSOL)."""
    sig = "sig_wsol_swap_123"
    token_mint = "MemeToken11111111111111111111111111111111111"
    buyer_wallet = "BuyerWallet22222222222222222222222222222222"

    tx = TransactionDetail(
        signature=sig,
        block_time=1716000200,
        signer=buyer_wallet,
        sol_balance_changes=[
            # Small gas fee in native SOL
            BalanceChange(
                address=buyer_wallet,
                pre_balance=1.0,
                post_balance=0.999995,
                change=-0.000005,
                decimals=9,
            )
        ],
        token_balance_changes=[
            # WSOL spent
            BalanceChange(
                address=buyer_wallet,
                pre_balance=5.0,
                post_balance=2.5,
                change=-2.5,  # -2.5 WSOL spent
                mint=WSOL_MINT,
                decimals=9,
            ),
            # Target token acquired
            BalanceChange(
                address=buyer_wallet,
                pre_balance=0.0,
                post_balance=85000.0,
                change=85000.0,
                mint=token_mint,
                decimals=9,
            ),
        ],
        programs=[RAYDIUM_CPMM_ID],
        status="Success",
    )

    result = classify_transaction(tx, wallet_address=buyer_wallet, token_address=token_mint)

    assert result.classification == ClassificationEnum.BUY
    assert result.confidence == ConfidenceEnum.HIGH
    # Net quote change includes WSOL + native gas
    assert pytest.approx(result.sol_change, 0.0001) == -2.500005
    assert result.token_change == 85000.0


# =====================================================================
# 3. ORDINARY TRANSFER TEST (GAS FEE ONLY)
# =====================================================================

def test_standard_transfer_not_buy():
    """Verify that ordinary transfer with gas fee (0.000005 SOL) is classified as TRANSFER, NOT BUY."""
    sig = "sig_transfer_regular"
    token_mint = "TokenMint1111111111111111111111111111111111"
    sender_wallet = "SenderWallet1111111111111111111111111111111"
    receiver_wallet = "ReceiverWallet2222222222222222222222222222"

    tx = TransactionDetail(
        signature=sig,
        block_time=1716000300,
        signer=sender_wallet,
        sol_balance_changes=[
            # Sender only pays 0.000005 SOL gas fee
            BalanceChange(
                address=sender_wallet,
                pre_balance=2.0,
                post_balance=1.999995,
                change=-0.000005,
                decimals=9,
            )
        ],
        token_balance_changes=[
            BalanceChange(
                address=sender_wallet,
                pre_balance=1000.0,
                post_balance=0.0,
                change=-1000.0,
                mint=token_mint,
                decimals=9,
            ),
            BalanceChange(
                address=receiver_wallet,
                pre_balance=0.0,
                post_balance=1000.0,
                change=1000.0,
                mint=token_mint,
                decimals=9,
            ),
        ],
        programs=[SYSTEM_PROGRAM_ID],  # No DEX
        status="Success",
    )

    # From sender's perspective: sent tokens, paid gas -> TRANSFER
    res_sender = classify_transaction(tx, wallet_address=sender_wallet, token_address=token_mint)
    assert res_sender.classification in (ClassificationEnum.TRANSFER, ClassificationEnum.TRANSFER_OUT)
    assert res_sender.confidence == ConfidenceEnum.HIGH

    # From receiver's perspective: received tokens, paid 0 gas -> DISTRIBUTION
    res_receiver = classify_transaction(tx, wallet_address=receiver_wallet, token_address=token_mint)
    assert res_receiver.classification == ClassificationEnum.DISTRIBUTION
    assert res_receiver.confidence == ConfidenceEnum.HIGH

    # Ensure neither was misclassified as BUY!
    assert res_sender.classification != ClassificationEnum.BUY
    assert res_receiver.classification != ClassificationEnum.BUY


# =====================================================================
# 4. DISTRIBUTION TEST
# =====================================================================

def test_distribution_airdrop():
    """Verify DISTRIBUTION when recipient receives tokens without any quote asset spent."""
    sig = "sig_airdrop_distribution"
    token_mint = "TokenMint1111111111111111111111111111111111"
    receiver_wallet = "AirdropRecipient111111111111111111111111111"

    tx = TransactionDetail(
        signature=sig,
        block_time=1716000400,
        signer="DistributorAuthority1111111111111111111111",
        sol_balance_changes=[],  # Recipient spent 0 SOL
        token_balance_changes=[
            BalanceChange(
                address=receiver_wallet,
                pre_balance=0.0,
                post_balance=50000.0,
                change=50000.0,
                mint=token_mint,
                decimals=9,
            )
        ],
        programs=[SYSTEM_PROGRAM_ID],  # No DEX
        status="Success",
    )

    result = classify_transaction(tx, wallet_address=receiver_wallet, token_address=token_mint)
    assert result.classification == ClassificationEnum.DISTRIBUTION
    assert result.confidence == ConfidenceEnum.HIGH
    assert result.token_change == 50000.0
    assert result.sol_change == 0.0


# =====================================================================
# 5. SELL & FAILED TRANSACTION TESTS
# =====================================================================

def test_sell_transaction():
    """Verify SELL classification when token is sent out and quote asset increases."""
    sig = "sig_sell_swap"
    token_mint = "TokenMint1111111111111111111111111111111111"
    seller_wallet = "SellerWallet1111111111111111111111111111111"

    tx = TransactionDetail(
        signature=sig,
        block_time=1716000500,
        signer=seller_wallet,
        sol_balance_changes=[
            BalanceChange(
                address=seller_wallet,
                pre_balance=2.0,
                post_balance=5.2,
                change=3.2,  # Received 3.2 SOL
                decimals=9,
            )
        ],
        token_balance_changes=[
            BalanceChange(
                address=seller_wallet,
                pre_balance=50000.0,
                post_balance=0.0,
                change=-50000.0,  # Sold 50,000 tokens
                mint=token_mint,
                decimals=9,
            )
        ],
        programs=[PUMP_FUN_PROGRAM_ID],
        status="Success",
    )

    result = classify_transaction(tx, wallet_address=seller_wallet, token_address=token_mint)
    assert result.classification == ClassificationEnum.SELL
    assert result.confidence == ConfidenceEnum.HIGH
    assert result.sol_change == 3.2
    assert result.token_change == -50000.0


def test_failed_transaction():
    """Verify that failed on-chain transactions are assigned UNKNOWN status."""
    sig = "sig_failed_tx"
    tx = TransactionDetail(
        signature=sig,
        block_time=1716000600,
        signer="Wallet1",
        status="Failed",
    )

    result = classify_transaction(tx, wallet_address="Wallet1", token_address="Token1")
    assert result.classification == ClassificationEnum.UNKNOWN
    assert result.confidence == ConfidenceEnum.UNKNOWN


# =====================================================================
# 6. SQLITE CACHING TEST FOR get_transaction_details_batch
# =====================================================================

def test_get_transaction_details_batch_sqlite_caching(memory_db):
    """Verify that repeated calls to get_transaction_details_batch read from SQLite without network call."""
    mock_client = MagicMock(spec=SolscanClient)
    sig = "sig_test_caching_001"

    raw_solscan_payload = {
        "success": True,
        "data": {
            "block_time": 1716000700,
            "signer": ["WalletBuyer1111111111111111111111111111111"],
            "status": "Success",
            "sol_bal_change": [
                {
                    "address": "WalletBuyer111111111111111111111111111",
                    "pre_balance": 5.0,
                    "post_balance": 3.0,
                    "change": -2.0,
                }
            ],
            "token_bal_change": [],
            "programs_involved": [PUMP_FUN_PROGRAM_ID],
        },
    }

    mock_client.get_transaction_detail.return_value = raw_solscan_payload

    # First call: Not in SQLite, must call mock_client
    details1 = get_transaction_details_batch([sig], client=mock_client, db=memory_db)
    assert len(details1) == 1
    assert details1[0].signature == sig
    assert details1[0].block_time == 1716000700
    assert mock_client.get_transaction_detail.call_count == 1

    # Verify signature exists in SQLite transactions table
    assert memory_db.has_transaction(sig)

    # Second call: Must hit SQLite transactions table, NO network call to mock_client
    details2 = get_transaction_details_batch([sig], client=mock_client, db=memory_db)
    assert len(details2) == 1
    assert details2[0].signature == sig
    assert details2[0].block_time == 1716000700
    assert mock_client.get_transaction_detail.call_count == 1  # Did NOT increment!
