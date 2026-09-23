"""Regression test suite for Bonding Curve PDA exclusion and SELL swap validation."""

from models.schemas import BalanceChange, ClassificationEnum, ConfidenceEnum, TransactionDetail
from analyzers.transaction_classifier import classify_transaction
from analyzers.candidate_generator import (
    filter_candidate_events,
    extract_candidate_wallets,
    get_token_pool_addresses,
)
from analyzers.wallet_analyzer import build_buyer_profile
from models.schemas import TransferEvent


def test_bonding_curve_pda_derivation():
    """Verify Pump.fun bonding curve PDA and ATA derivation."""
    token_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"
    pools = get_token_pool_addresses(token_mint)
    # 5PnLRh... is the bonding curve PDA for FIBONACCI
    assert "5PnLRhNdRKURUCPezx4nNBpBGfkTe1kVFYELKWMFvRRd" in pools
    # 3a3dQo... is the bonding curve ATA
    assert "3a3dQoBpL9pBfE6bCRgB9yk68bHx7LVzKst8L4nnSqph" in pools


def test_candidate_filtering_excludes_bonding_curve():
    """Verify filter_candidate_events eliminates the bonding curve PDA."""
    token_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"
    curve_pda = "5PnLRhNdRKURUCPezx4nNBpBGfkTe1kVFYELKWMFvRRd"
    signer = "DN19MDuZRKsj4RG26sXtUaoPuQenHkxyQmGQDbgVgmzv"

    event = TransferEvent(
        signature="2Tf2bRoQoBp7BW6CfqwskimB7Lt9umGV6P8EuLXGhhwN3mnz9S1XdSHrE12U1KHkk3dQ3fqS91VQEnqrwzryKTPm",
        block_time=1790065256,
        from_address=signer,
        to_address=curve_pda,
        token_address=token_mint,
        amount=4012469.902709,
        decimals=6,
        slot=449323079,
    )

    filtered = filter_candidate_events([event], token_address=token_mint)
    assert len(filtered) == 0

    candidates = extract_candidate_wallets(filtered, token_address=token_mint)
    assert curve_pda not in candidates


def test_tx_classifier_rejects_pool_as_buyer():
    """Verify classify_transaction rejects the bonding curve PDA as a buyer."""
    token_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"
    curve_pda = "5PnLRhNdRKURUCPezx4nNBpBGfkTe1kVFYELKWMFvRRd"
    signer = "DN19MDuZRKsj4RG26sXtUaoPuQenHkxyQmGQDbgVgmzv"

    tx = TransactionDetail(
        signature="2Tf2bRoQoBp7BW6CfqwskimB7Lt9umGV6P8EuLXGhhwN3mnz9S1XdSHrE12U1KHkk3dQ3fqS91VQEnqrwzryKTPm",
        block_time=1790065256,
        slot=449323079,
        signer=signer,
        sol_balance_changes=[
            BalanceChange(address=signer, pre_balance=6.53, post_balance=6.87, change=0.342),
            BalanceChange(address=curve_pda, pre_balance=22.91, post_balance=22.57, change=-0.346),
        ],
        token_balance_changes=[
            BalanceChange(address=signer, pre_balance=4012469.9, post_balance=0.0, change=-4012469.9, mint=token_mint),
            BalanceChange(address=curve_pda, pre_balance=535.3, post_balance=539.3, change=4012469.9, mint=token_mint),
        ],
        programs=["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"],
        status="Success",
    )

    # 1. Perspective of curve_pda -> Must NOT be BUY
    curve_class = classify_transaction(tx, wallet_address=curve_pda, token_address=token_mint)
    assert curve_class.classification != ClassificationEnum.BUY
    assert curve_class.classification == ClassificationEnum.UNKNOWN

    # 2. Perspective of signer -> Must be SELL
    signer_class = classify_transaction(tx, wallet_address=signer, token_address=token_mint)
    assert signer_class.classification == ClassificationEnum.SELL
    assert signer_class.confidence == ConfidenceEnum.HIGH

    # 3. Buyer profile for curve_pda -> Must be None (no verified buy)
    profile = build_buyer_profile(
        wallet_address=curve_pda,
        token_address=token_mint,
        launch_time=1790065256,
        tx_classifications=[curve_class],
    )
    assert profile is None


def test_dn19_full_lifecycle_buy_then_sell():
    """Verify that a buyer who buys and then sells within the scan window
    gets properly profiled with verified BUY, verified SELL, and 100% exit ratio."""
    from analyzers.sell_analyzer import enrich_profile_with_sells
    token_mint = "5gNhoFFz6UuyWugjiMc1fiuixrH8NvibMFbDKYGKr1Mc"
    curve_pda = "5PnLRhNdRKURUCPezx4nNBpBGfkTe1kVFYELKWMFvRRd"
    dn19 = "DN19MDuZRKsj4RG26sXtUaoPuQenHkxyQmGQDbgVgmzv"

    tx_buy = TransactionDetail(
        signature="5ffLcdDsKh5qkjr7JXLiQT6Zsn4h4VsFKLudwfwFY16h4EyYrXZmE7K9Ax8MHqDET5rXNmhmtG9GmFYW8iKRQ2pg",
        block_time=1790065255,
        slot=449323076,
        signer=dn19,
        sol_balance_changes=[
            BalanceChange(address=dn19, pre_balance=6.874, post_balance=6.532, change=-0.341),
            BalanceChange(address=curve_pda, pre_balance=21.64, post_balance=21.97, change=0.334),
        ],
        token_balance_changes=[
            BalanceChange(address=dn19, pre_balance=0.0, post_balance=4012469.9, change=4012469.9, mint=token_mint),
            BalanceChange(address=curve_pda, pre_balance=550.3, post_balance=546.3, change=-4012469.9, mint=token_mint),
        ],
        programs=["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"],
        status="Success",
    )

    tx_sell = TransactionDetail(
        signature="2Tf2bRoQoBp7BW6CfqwskimB7Lt9umGV6P8EuLXGhhwN3mnz9S1XdSHrE12U1KHkk3dQ3fqS91VQEnqrwzryKTPm",
        block_time=1790065256,
        slot=449323079,
        signer=dn19,
        sol_balance_changes=[
            BalanceChange(address=dn19, pre_balance=6.532, post_balance=6.875, change=0.342),
            BalanceChange(address=curve_pda, pre_balance=22.91, post_balance=22.57, change=-0.346),
        ],
        token_balance_changes=[
            BalanceChange(address=dn19, pre_balance=4012469.9, post_balance=0.0, change=-4012469.9, mint=token_mint),
            BalanceChange(address=curve_pda, pre_balance=535.3, post_balance=539.3, change=4012469.9, mint=token_mint),
        ],
        programs=["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"],
        status="Success",
    )

    c_buy = classify_transaction(tx_buy, wallet_address=dn19, token_address=token_mint)
    c_sell = classify_transaction(tx_sell, wallet_address=dn19, token_address=token_mint)

    assert c_buy.classification == ClassificationEnum.BUY
    assert c_sell.classification == ClassificationEnum.SELL

    profile = build_buyer_profile(
        wallet_address=dn19,
        token_address=token_mint,
        launch_time=1790065255,
        tx_classifications=[c_buy, c_sell],
        holder_data={"amount": 0.0, "rank": None, "percentage": 0.0},
    )
    assert profile is not None
    assert profile.buy_count == 1
    assert profile.first_buy_amount == 4012469.9
    assert profile.first_buy_signature == tx_buy.signature

    enrich_profile_with_sells(profile, [c_buy, c_sell])
    assert profile.sell_count == 1
    assert profile.total_sell_amount == 4012469.9
    assert profile.exit_ratio == 1.0
    assert profile.current_holding == 0.0

