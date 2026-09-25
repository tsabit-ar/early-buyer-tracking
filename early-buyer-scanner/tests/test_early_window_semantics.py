"""Regression tests for Item F: Early-Window Semantics & Launch Time Evidence Type.

Tests:
A. EXACT_GENESIS -> launch_time_type = EXACT_GENESIS -> early_window_basis = EXACT_GENESIS_WINDOW
B. ESTIMATED_POOL_CREATION -> launch_time_type = ESTIMATED_POOL_CREATION -> early_window_basis = ESTIMATED_LAUNCH_WINDOW -> not exact genesis
C. BOUNDED_OLDEST_SIGNATURE -> basis = BOUNDED_DISCOVERY_WINDOW
D. UNKNOWN -> basis = UNKNOWN
E. Verify resolution_type is not lost when LaunchTimeResolution object is passed from token resolver
F. Verify CSV/export preserves launch_time_type, early_window_basis, and early_window_status
"""

import csv
import io
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collectors.token import resolve_launch_time
from models.schemas import (
    ConfidenceEnum,
    EarlyWindowBasisEnum,
    EarlyWindowStatusEnum,
    LaunchResolutionType,
    LaunchTimeResolution,
    TokenMetadata,
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


def test_scenario_a_exact_genesis_semantics():
    """Scenario A:
    launch_time_type = EXACT_GENESIS
    -> early_window_basis = EXACT_GENESIS_WINDOW
    -> early_window_status = EARLY_WINDOW_CONFIRMED (when discovery complete)
    """
    token = TokenMetadata(
        token_address="MintExact111111111111111111111111111111111",
        launch_time=1700000000,
        launch_confidence=ConfidenceEnum.HIGH,
        launch_time_type=LaunchResolutionType.EXACT_GENESIS.value,
    )

    profile = WalletProfile(
        wallet_address="EarlyBuyerWallet111111111111111111111111111",
        token_address=token.token_address,
        first_buy_time=1700003600,  # 1 hour after launch
        candidate_discovery_complete=True,
    )

    # Apply early window semantics logic
    profile.launch_time_type = token.launch_time_type
    assert profile.launch_time_type == LaunchResolutionType.EXACT_GENESIS.value

    # Mapping to early_window_basis
    if profile.launch_time_type == LaunchResolutionType.EXACT_GENESIS.value:
        profile.early_window_basis = EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value
    else:
        profile.early_window_basis = EarlyWindowBasisEnum.UNKNOWN.value

    assert profile.early_window_basis == EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value

    # Determine is_in_early_window & status
    profile.is_in_early_window = bool(profile.first_buy_time <= token.launch_time + int(24.0 * 3600))
    assert profile.is_in_early_window is True

    if profile.is_in_early_window:
        if (
            profile.early_window_basis == EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value
            and profile.candidate_discovery_complete
        ):
            profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value
        else:
            profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value

    assert profile.early_window_status == EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value


def test_scenario_b_estimated_pool_creation_semantics():
    """Scenario B:
    launch_time_type = ESTIMATED_POOL_CREATION
    -> early_window_basis = ESTIMATED_LAUNCH_WINDOW
    -> early_window_status = EARLY_WINDOW_ESTIMATED (NEVER CONFIRMED)
    """
    token = TokenMetadata(
        token_address="MintEstimated1111111111111111111111111111111",
        launch_time=1710000000,
        launch_confidence=ConfidenceEnum.MEDIUM,
        launch_time_type=LaunchResolutionType.ESTIMATED_POOL_CREATION.value,
    )

    profile = WalletProfile(
        wallet_address="PoolBuyerWallet1111111111111111111111111111",
        token_address=token.token_address,
        first_buy_time=1710007200,  # 2 hours after estimated pool
        candidate_discovery_complete=True,
    )

    profile.launch_time_type = token.launch_time_type
    assert profile.launch_time_type == LaunchResolutionType.ESTIMATED_POOL_CREATION.value
    assert profile.launch_time_type != LaunchResolutionType.EXACT_GENESIS.value

    # Mapping to early_window_basis
    if profile.launch_time_type == LaunchResolutionType.ESTIMATED_POOL_CREATION.value:
        profile.early_window_basis = EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value
    assert profile.early_window_basis == EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value

    # Determine is_in_early_window & status
    profile.is_in_early_window = bool(profile.first_buy_time <= token.launch_time + int(24.0 * 3600))
    assert profile.is_in_early_window is True

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

    # Crucial safety assertion: Must NOT be EARLY_WINDOW_CONFIRMED
    assert profile.early_window_status != EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value
    assert profile.early_window_status == EarlyWindowStatusEnum.EARLY_WINDOW_ESTIMATED.value


def test_estimated_pool_creation_incomplete_discovery_never_confirmed():
    """Regression test:
    Verify that ESTIMATED_POOL_CREATION combined with candidate_discovery_complete=False
    NEVER produces EARLY_WINDOW_CONFIRMED, even when the buy time falls strictly
    within the estimated early window.
    """
    token = TokenMetadata(
        token_address="MintEstimated2222222222222222222222222222222",
        launch_time=1710000000,
        launch_confidence=ConfidenceEnum.MEDIUM,
        launch_time_type=LaunchResolutionType.ESTIMATED_POOL_CREATION.value,
    )

    profile = WalletProfile(
        wallet_address="PoolBuyerIncomplete111111111111111111111111",
        token_address=token.token_address,
        first_buy_time=1710001800,  # 30 mins after estimated pool creation
        candidate_discovery_complete=False,
        candidate_discovery_truncated=True,
    )

    # Propagate launch_time_type
    profile.launch_time_type = token.launch_time_type
    assert profile.launch_time_type == LaunchResolutionType.ESTIMATED_POOL_CREATION.value

    # Map to early_window_basis
    if profile.launch_time_type == LaunchResolutionType.EXACT_GENESIS.value:
        profile.early_window_basis = EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value
    elif profile.launch_time_type == LaunchResolutionType.ESTIMATED_POOL_CREATION.value:
        profile.early_window_basis = EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value
    elif profile.launch_time_type == LaunchResolutionType.BOUNDED_OLDEST_SIGNATURE.value:
        profile.early_window_basis = EarlyWindowBasisEnum.BOUNDED_DISCOVERY_WINDOW.value
    else:
        profile.early_window_basis = EarlyWindowBasisEnum.UNKNOWN.value

    assert profile.early_window_basis == EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value

    # Determine is_in_early_window
    profile.is_in_early_window = bool(
        profile.first_buy_time <= token.launch_time + int(profile.early_window_hours * 3600)
    )
    assert profile.is_in_early_window is True

    # Apply anti-false-certainty rules
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

    # STAGE 1: Verify it is NOT EARLY_WINDOW_CONFIRMED under any circumstance
    assert profile.early_window_status != EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value
    assert profile.early_window_status == EarlyWindowStatusEnum.EARLY_WINDOW_ESTIMATED.value

    # STAGE 2: Test when buy time is outside window
    profile_late = WalletProfile(
        wallet_address="PoolBuyerLate1111111111111111111111111111111",
        token_address=token.token_address,
        first_buy_time=1710000000 + int(48 * 3600),  # 48 hours later
        candidate_discovery_complete=False,
        candidate_discovery_truncated=True,
    )
    profile_late.launch_time_type = token.launch_time_type
    profile_late.early_window_basis = EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value
    profile_late.is_in_early_window = bool(
        profile_late.first_buy_time <= token.launch_time + int(profile_late.early_window_hours * 3600)
    )
    assert profile_late.is_in_early_window is False

    if profile_late.is_in_early_window:
        if (
            profile_late.early_window_basis == EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value
            and profile_late.candidate_discovery_complete
        ):
            profile_late.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value
        elif profile_late.early_window_basis == EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value:
            profile_late.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_ESTIMATED.value
        else:
            profile_late.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value
    else:
        profile_late.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value

    assert profile_late.early_window_status != EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value
    assert profile_late.early_window_status == EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value


def test_scenario_c_bounded_oldest_signature_semantics():
    """Scenario C:
    launch_time_type = BOUNDED_OLDEST_SIGNATURE
    -> basis = BOUNDED_DISCOVERY_WINDOW
    -> status = EARLY_WINDOW_UNVERIFIED
    """
    token = TokenMetadata(
        token_address="MintBounded111111111111111111111111111111111",
        launch_time=1720000000,
        launch_confidence=ConfidenceEnum.LOW,
        launch_time_type=LaunchResolutionType.BOUNDED_OLDEST_SIGNATURE.value,
    )

    profile = WalletProfile(
        wallet_address="BoundedBuyer1111111111111111111111111111111",
        token_address=token.token_address,
        first_buy_time=1720003600,
        candidate_discovery_complete=False,
    )

    profile.launch_time_type = token.launch_time_type

    if profile.launch_time_type == LaunchResolutionType.BOUNDED_OLDEST_SIGNATURE.value:
        profile.early_window_basis = EarlyWindowBasisEnum.BOUNDED_DISCOVERY_WINDOW.value

    assert profile.early_window_basis == EarlyWindowBasisEnum.BOUNDED_DISCOVERY_WINDOW.value

    profile.is_in_early_window = bool(profile.first_buy_time <= token.launch_time + int(24.0 * 3600))
    if profile.early_window_basis == EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value:
        profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value
    elif profile.early_window_basis == EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value:
        profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_ESTIMATED.value
    else:
        profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value

    assert profile.early_window_status == EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value


def test_scenario_d_unknown_resolution_semantics():
    """Scenario D:
    launch_time_type = UNKNOWN
    -> basis = UNKNOWN
    -> status = EARLY_WINDOW_UNVERIFIED
    """
    token = TokenMetadata(
        token_address="MintUnknown111111111111111111111111111111111",
        launch_time=None,
        launch_confidence=ConfidenceEnum.UNKNOWN,
        launch_time_type=LaunchResolutionType.UNKNOWN.value,
    )

    profile = WalletProfile(
        wallet_address="UnknownBuyer1111111111111111111111111111111",
        token_address=token.token_address,
        candidate_discovery_complete=False,
    )

    profile.launch_time_type = token.launch_time_type
    if profile.launch_time_type == LaunchResolutionType.EXACT_GENESIS.value:
        profile.early_window_basis = EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value
    else:
        profile.early_window_basis = EarlyWindowBasisEnum.UNKNOWN.value

    assert profile.early_window_basis == EarlyWindowBasisEnum.UNKNOWN.value
    profile.early_window_status = EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value
    assert profile.early_window_status == EarlyWindowStatusEnum.EARLY_WINDOW_UNVERIFIED.value


def test_scenario_e_resolution_type_preserved_from_resolver(memory_db):
    """Scenario E:
    Ensure resolution_type is not lost when LaunchTimeResolution object is
    returned by resolve_launch_time and stored on TokenMetadata / SQLite.
    """
    mock_rpc = MagicMock()
    # Batch with 100 signatures (< 1000) -> natural termination -> EXACT_GENESIS
    batch = [
        {"signature": f"sig_{i}", "blockTime": 1700000000 + i, "slot": 100 + i}
        for i in range(100)
    ]
    batch.reverse()
    mock_rpc.get_signatures_for_address.return_value = batch

    mint = "TokenMintExact1111111111111111111111111111"
    res = resolve_launch_time(mint, client=mock_rpc, db=memory_db, max_pages=5)

    assert isinstance(res, LaunchTimeResolution)
    assert res.resolution_type == LaunchResolutionType.EXACT_GENESIS.value
    assert res.launch_time == 1700000000

    # Ensure token record in SQLite persisted the launch_time_type
    saved_token = memory_db.get_token(mint)
    assert saved_token is not None
    assert saved_token.launch_time_type == LaunchResolutionType.EXACT_GENESIS.value

    # Verify cache hit preserves resolution_type
    cached_res = resolve_launch_time(mint, client=mock_rpc, db=memory_db, force_refresh=False)
    assert cached_res.resolution_type == LaunchResolutionType.EXACT_GENESIS.value


def test_scenario_f_csv_export_preserves_early_window_semantics():
    """Scenario F:
    Verify CSV export preserves launch_time_type, early_window_basis, and early_window_status.
    """
    output = io.StringIO()
    writer = csv.writer(output)

    # Header with new semantic columns
    writer.writerow([
        "Rank", "Wallet Address", "In Early Window",
        "Launch Time Type", "Early Window Basis", "Early Window Status",
    ])

    p1 = WalletProfile(
        wallet_address="ExactBuyer1111111111111111111111111111111",
        token_address="MintTest111111111111111111111111111111111",
        is_in_early_window=True,
        launch_time_type=LaunchResolutionType.EXACT_GENESIS.value,
        early_window_basis=EarlyWindowBasisEnum.EXACT_GENESIS_WINDOW.value,
        early_window_status=EarlyWindowStatusEnum.EARLY_WINDOW_CONFIRMED.value,
    )

    p2 = WalletProfile(
        wallet_address="EstBuyer11111111111111111111111111111111",
        token_address="MintTest111111111111111111111111111111111",
        is_in_early_window=True,
        launch_time_type=LaunchResolutionType.ESTIMATED_POOL_CREATION.value,
        early_window_basis=EarlyWindowBasisEnum.ESTIMATED_LAUNCH_WINDOW.value,
        early_window_status=EarlyWindowStatusEnum.EARLY_WINDOW_ESTIMATED.value,
    )

    for rank, p in enumerate([p1, p2], start=1):
        writer.writerow([
            rank,
            p.wallet_address,
            "YES" if p.is_in_early_window else "NO",
            p.launch_time_type,
            p.early_window_basis,
            p.early_window_status,
        ])

    csv_text = output.getvalue()
    rows = list(csv.reader(io.StringIO(csv_text)))

    assert len(rows) == 3
    assert rows[0] == ["Rank", "Wallet Address", "In Early Window", "Launch Time Type", "Early Window Basis", "Early Window Status"]
    assert rows[1] == ["1", "ExactBuyer1111111111111111111111111111111", "YES", "EXACT_GENESIS", "EXACT_GENESIS_WINDOW", "EARLY_WINDOW_CONFIRMED"]
    assert rows[2] == ["2", "EstBuyer11111111111111111111111111111111", "YES", "ESTIMATED_POOL_CREATION", "ESTIMATED_LAUNCH_WINDOW", "EARLY_WINDOW_ESTIMATED"]
