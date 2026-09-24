"""Data schemas for Early Buyer Scanner (EBRS).

Defines Pydantic v2 validation models and domain enums.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClassificationEnum(str, Enum):
    """Transaction classifications defined by PRD Section 7 (FR-07)."""
    BUY = "BUY"
    SELL = "SELL"
    TRANSFER = "TRANSFER"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
    BURN = "BURN"
    CLOSE_ACCOUNT = "CLOSE_ACCOUNT"
    DISTRIBUTION = "DISTRIBUTION"
    UNKNOWN = "UNKNOWN"


class ConfidenceEnum(str, Enum):
    """Confidence levels defined by PRD Section 10."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class LaunchResolutionType(str, Enum):
    """Resolution method/source for token launch or genesis time."""
    EXACT_GENESIS = "EXACT_GENESIS"
    ESTIMATED_POOL_CREATION = "ESTIMATED_POOL_CREATION"
    BOUNDED_OLDEST_SIGNATURE = "BOUNDED_OLDEST_SIGNATURE"
    CACHED_DB = "CACHED_DB"
    UNKNOWN = "UNKNOWN"


class DiscoverySourceEnum(str, Enum):
    """Source of candidate discovery transfers (Phase 3)."""
    SOLSCAN_ASC = "SOLSCAN_ASC"
    NATIVE_RPC_GENESIS = "NATIVE_RPC_GENESIS"
    NATIVE_RPC_BOUNDED = "NATIVE_RPC_BOUNDED"
    UNKNOWN = "UNKNOWN"


class LaunchTimeResolution(BaseModel):
    """Result of launch time resolution with forensic observability and backward compatibility.
    
    Provides tuple unpacking support (launch_time, confidence) for legacy callers,
    while offering rich forensic metadata (evidence_signature, resolution_type, etc.)
    for advanced observability.
    """
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    token_address: str = Field(..., description="Solana mint address")
    launch_time: Optional[int] = Field(default=None, description="Unix timestamp of launch (seconds)")
    confidence: ConfidenceEnum = Field(default=ConfidenceEnum.LOW, description="Confidence level")
    resolution_type: str = Field(default=LaunchResolutionType.UNKNOWN.value, description="Method used to resolve launch time")
    evidence_signature: Optional[str] = Field(default=None, description="Signature providing timestamp evidence")
    evidence_slot: Optional[int] = Field(default=None, description="Block slot of evidence signature")
    evidence_details: Optional[str] = Field(default=None, description="Forensic explanation of evidence")
    pages_fetched: int = Field(default=0, description="Total RPC signature pages fetched")
    signatures_fetched: int = Field(default=0, description="Total signatures fetched during resolution")
    elapsed_seconds: float = Field(default=0.0, description="Execution time taken in seconds")
    termination_reason: str = Field(default="", description="Reason pagination terminated")

    def __iter__(self):
        """Allows 100% backward compatibility unpacking:
        launch_time, launch_conf = resolve_launch_time(...)
        """
        yield self.launch_time
        yield self.confidence.value if hasattr(self.confidence, "value") else str(self.confidence)

    def __getitem__(self, index: int):
        if index == 0:
            return self.launch_time
        elif index == 1:
            return self.confidence.value if hasattr(self.confidence, "value") else str(self.confidence)
        raise IndexError("LaunchTimeResolution only supports index 0 (launch_time) and 1 (confidence).")

    def __len__(self) -> int:
        return 2


class TokenMetadata(BaseModel):
    """Token profile and launch metadata (FR-01, FR-02)."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    token_address: str = Field(..., description="Solana mint address")
    name: Optional[str] = Field(default=None, description="Token name")
    symbol: Optional[str] = Field(default=None, description="Token symbol")
    decimals: int = Field(default=9, ge=0, le=18, description="Token decimals")
    creator: Optional[str] = Field(default=None, description="Creator or deployer address")
    launch_time: Optional[int] = Field(default=None, description="Unix timestamp of launch")
    launch_confidence: ConfidenceEnum = Field(
        default=ConfidenceEnum.LOW,
        description="Confidence of detected launch time"
    )
    created_at: Optional[str] = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Record creation timestamp"
    )


class TransferEvent(BaseModel):
    """Individual token transfer event from historical logs (FR-03)."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    signature: str = Field(..., description="Transaction signature")
    block_time: int = Field(..., description="Block unix timestamp")
    from_address: str = Field(..., description="Sender address")
    to_address: str = Field(..., description="Recipient address")
    token_address: str = Field(..., description="Token mint address")
    amount: float = Field(..., ge=0.0, description="Token amount transferred")
    decimals: int = Field(default=9, ge=0, le=18, description="Token decimals")
    activity_type: Optional[str] = Field(default=None, description="Activity type if given by API")


class BalanceChange(BaseModel):
    """Pre and post balance state for an account in a transaction."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    address: str = Field(..., description="Account address")
    pre_balance: float = Field(..., description="Balance before transaction")
    post_balance: float = Field(..., description="Balance after transaction")
    change: float = Field(..., description="post_balance - pre_balance")
    mint: Optional[str] = Field(default=None, description="Token mint address or None for SOL")
    decimals: int = Field(default=9, ge=0, le=18, description="Asset decimals")


class TransactionDetail(BaseModel):
    """Parsed on-chain transaction data with balances and programs (FR-06)."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    signature: str = Field(..., description="Solana transaction signature")
    block_time: int = Field(..., description="Block unix timestamp")
    slot: Optional[int] = Field(default=None, description="Solana slot/block number")
    signer: str = Field(..., description="Primary fee-payer / signer address")
    sol_balance_changes: List[BalanceChange] = Field(
        default_factory=list,
        description="SOL balance changes for involved accounts"
    )
    token_balance_changes: List[BalanceChange] = Field(
        default_factory=list,
        description="Token balance changes for involved accounts"
    )
    programs: List[str] = Field(
        default_factory=list,
        description="Involved program IDs in transaction and inner instructions"
    )
    status: str = Field(default="Success", description="Execution status")
    priority_fee: Optional[float] = Field(default=None, ge=0.0, description="Priority fee in SOL")
    raw_data: Optional[Dict[str, Any]] = Field(default=None, description="Full raw payload if cached")


class TransactionClassification(BaseModel):
    """Classification outcome and rationale for a candidate event (FR-07)."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    signature: str = Field(..., description="Solana transaction signature")
    wallet: str = Field(..., description="Candidate wallet address")
    token_address: str = Field(..., description="Token mint address")
    block_time: int = Field(default=0, description="Block timestamp")
    slot: Optional[int] = Field(default=None, description="Solana slot/block number")
    classification: ClassificationEnum = Field(..., description="BUY, SELL, etc.")
    confidence: ConfidenceEnum = Field(..., description="HIGH, MEDIUM, etc.")
    reasons: List[str] = Field(default_factory=list, description="Audit trail of why this classification was assigned")
    sol_change: float = Field(default=0.0, description="Wallet SOL balance change (negative = spent)")
    token_change: float = Field(default=0.0, description="Wallet token balance change (positive = received)")
    programs: List[str] = Field(default_factory=list, description="DEX/AMM programs detected")


class WalletProfile(BaseModel):
    """Comprehensive early buyer behavior profile (FR-08 - FR-12)."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    wallet_address: str = Field(..., description="Wallet public key")
    token_address: str = Field(..., description="Token mint address")
    first_buy_time: Optional[int] = Field(default=None, description="Unix timestamp of first verified buy")
    first_buy_slot: Optional[int] = Field(default=None, description="Solana slot/block of first verified buy")
    first_buy_signature: Optional[str] = Field(default=None, description="Signature of first buy")
    first_buy_amount: float = Field(default=0.0, ge=0.0, description="Tokens received on first buy")
    time_after_launch: Optional[int] = Field(
        default=None,
        description="Seconds elapsed between token launch and first buy"
    )
    total_buy_amount: float = Field(default=0.0, ge=0.0, description="Total tokens acquired across all buys")
    buy_count: int = Field(default=0, ge=0, description="Total number of verified buy transactions")
    sell_count: int = Field(default=0, ge=0, description="Total number of verified sell transactions")
    total_sell_amount: float = Field(default=0.0, ge=0.0, description="Total tokens sold")
    current_holding: float = Field(default=0.0, ge=0.0, description="Current token balance")
    exit_ratio: float = Field(default=0.0, ge=0.0, description="Ratio of sold to acquired tokens (0.0 to 1.0+)")
    holder_rank: Optional[int] = Field(default=None, ge=1, description="Current rank among holders")
    holder_percentage: Optional[float] = Field(default=None, ge=0.0, le=100.0, description="Current percent of supply held")
    score: float = Field(default=0.0, ge=0.0, le=100.0, description="Calculated EBRS score (0-100)")
    confidence: ConfidenceEnum = Field(default=ConfidenceEnum.UNKNOWN, description="Aggregated confidence")
    is_same_block_sniper: bool = Field(default=False, description="Flag indicating if wallet bought in same slot as other early buyers")
    wallet_age_days: Optional[float] = Field(default=None, description="Wallet age in days relative to token launch")
    is_fresh_wallet: bool = Field(default=False, description="True if wallet age < 24 hours at token launch")
    funder_address: Optional[str] = Field(default=None, description="Initial SOL funding source address")
    funder_type: str = Field(default="UNKNOWN", description="Funder category: CEX, EOA, INTERNAL, MATURE_WALLET, UNKNOWN")
    funding_amount_sol: Optional[float] = Field(default=None, description="Initial SOL funding amount received")
    funding_signature: Optional[str] = Field(default=None, description="Signature of initial funding transaction")
    cluster_id: Optional[str] = Field(default=None, description="Sybil cluster ID if sharing common funder")
    lifecycle_history_complete: bool = Field(default=True, description="Whether full transaction history was acquired without pagination cutoff")
    lifecycle_signature_count: int = Field(default=0, description="Total signatures fetched for candidate ATAs")
    lifecycle_pages_fetched: int = Field(default=0, ge=0, description="Number of RPC pagination pages fetched for ATAs")
    lifecycle_truncated: bool = Field(default=False, description="True if pagination hit safety limit while older transactions existed")
    truncation_reason: Optional[str] = Field(default=None, description="Explicit reason if lifecycle history was truncated")
    last_cursor_signature: Optional[str] = Field(default=None, description="Last transaction signature used as pagination cursor")
    lifecycle_atas: List[str] = Field(default_factory=list, description="Derived and scanned token accounts for this candidate")
    balance_reconciliation_status: str = Field(default="MATCH", description="Status of balance reconciliation: MATCH, MISMATCH, or INCOMPLETE")
    transfer_in_amount: float = Field(default=0.0, ge=0.0, description="Total tokens received via non-DEX transfers")
    transfer_out_amount: float = Field(default=0.0, ge=0.0, description="Total tokens sent via non-DEX transfers")
    net_transfer_amount: float = Field(default=0.0, description="Net transfer delta: transfer_in - transfer_out")
    burn_amount: float = Field(default=0.0, ge=0.0, description="Total tokens burned")
    unknown_in_amount: float = Field(default=0.0, ge=0.0, description="Unexplained positive token inflows")
    unknown_outflow_amount: float = Field(default=0.0, ge=0.0, description="Unexplained negative token outflows")
    reconciliation_difference: float = Field(default=0.0, description="Residual: total_inflow - total_valid_outflow - current_holding")
    unknown_tx_count: int = Field(default=0, ge=0, description="Count of non-zero UNKNOWN transactions")
    candidate_discovery_complete: bool = Field(default=True, description="Whether candidate discovery reached genesis or valid ascending history")
    candidate_discovery_truncated: bool = Field(default=False, description="Whether candidate discovery was cut off before genesis")
    genesis_reached: bool = Field(default=True, description="Whether token genesis block was reached during candidate discovery")
    discovery_source: str = Field(default=DiscoverySourceEnum.UNKNOWN.value, description="Source of candidate discovery")
    discovery_pages_fetched: int = Field(default=0, ge=0, description="Pages fetched during candidate discovery")
    discovery_signatures_fetched: int = Field(default=0, ge=0, description="Signatures fetched during candidate discovery")
    oldest_discovered_block_time: Optional[int] = Field(default=None, description="Block time of oldest discovered transaction")
    oldest_discovered_slot: Optional[int] = Field(default=None, description="Slot of oldest discovered transaction")
    discovery_termination_reason: Optional[str] = Field(default=None, description="Reason candidate discovery pagination stopped")
    candidate_discovery_status: str = Field(default="COMPLETE", description="COMPLETE or DISCOVERY_INCOMPLETE")
    is_in_early_window: bool = Field(default=True, description="Whether first buy falls within early window hours from launch")
    early_window_hours: float = Field(default=24.0, description="Configured early window in hours")
    evidence_signatures: List[str] = Field(default_factory=list, description="Transaction signatures supporting this profile")

    @field_validator("exit_ratio")
    @classmethod
    def validate_exit_ratio(cls, v: float) -> float:
        return max(0.0, round(v, 6))


class ScoreBreakdown(BaseModel):
    """Transparent scoring components breakdown (PRD Section 8)."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    wallet_address: str = Field(..., description="Wallet address")
    early_entry_score: float = Field(..., ge=0.0, le=100.0, description="Early entry component (0-100)")
    buy_size_score: float = Field(..., ge=0.0, le=100.0, description="Buy size percentile component (0-100)")
    accumulation_score: float = Field(..., ge=0.0, le=100.0, description="Accumulation component (0-100)")
    holding_score: float = Field(..., ge=0.0, le=100.0, description="Holding/retention component (0-100)")
    final_score: float = Field(..., ge=0.0, le=100.0, description="Weighted composite score (0-100)")
    breakdown_details: Dict[str, Any] = Field(default_factory=dict, description="Audit details and raw inputs")


class ScannerReport(BaseModel):
    """Consolidated report output (PRD Section 20)."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    token: TokenMetadata = Field(..., description="Token metadata")
    candidates_count: int = Field(..., ge=0, description="Total unique candidate wallets detected")
    likely_buyers_count: int = Field(..., ge=0, description="Total wallets verified as likely buyers")
    buyers: List[WalletProfile] = Field(default_factory=list, description="Ranked list of early buyer profiles")
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="Report generation timestamp"
    )
