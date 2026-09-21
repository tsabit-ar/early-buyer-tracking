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
    DISTRIBUTION = "DISTRIBUTION"
    UNKNOWN = "UNKNOWN"


class ConfidenceEnum(str, Enum):
    """Confidence levels defined by PRD Section 10."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


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
