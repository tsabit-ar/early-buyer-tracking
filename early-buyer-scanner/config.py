"""Configuration module for Early Buyer Scanner (EBRS).

Handles environment variables, Solana constants, DEX program IDs,
and default scoring weights as defined in PRD Section 8.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Dict, Set
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# ==========================================
# SOLANA ADDRESS CONSTANTS
# ==========================================

WSOL_MINT = "So11111111111111111111111111111111111111112"

# System and Token Programs
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

# Known DEX / AMM Programs
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_FUN_FEE_MIGRATION_ID = "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM"

RAYDIUM_AMM_V4_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_CPMM_ID = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_CLMM_ID = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
RAYDIUM_ROUTING_ID = "routeUGWgMr8bhqaxTiLLvtqcU6JeEGdETnTq4qadix"

METEORA_DLMM_ID = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
METEORA_POOLS_ID = "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB"

ORCA_WHIRLPOOL_ID = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
JUPITER_V6_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

# Set of DEX / AMM Programs for fast lookup during classification
KNOWN_DEX_PROGRAMS: Set[str] = {
    PUMP_FUN_PROGRAM_ID,
    PUMP_FUN_FEE_MIGRATION_ID,
    RAYDIUM_AMM_V4_ID,
    RAYDIUM_CPMM_ID,
    RAYDIUM_CLMM_ID,
    RAYDIUM_ROUTING_ID,
    METEORA_DLMM_ID,
    METEORA_POOLS_ID,
    ORCA_WHIRLPOOL_ID,
    JUPITER_V6_ID,
}

# Known non-buy entity addresses (burn, null, system, standard token contracts)
KNOWN_NON_BUY_ADDRESSES: Set[str] = {
    SYSTEM_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
    PUMP_FUN_FEE_MIGRATION_ID,
    "11111111111111111111111111111111",
    "deaddeaddeaddeaddeaddeaddeaddeaddeaddeaddead",
}

# ==========================================
# DEFAULT SCORING WEIGHTS & PARAMETERS
# ==========================================

DEFAULT_SCORING_WEIGHTS: Dict[str, float] = {
    "early_entry": 0.40,
    "buy_size": 0.25,
    "accumulation": 0.15,
    "holding_exit": 0.20,
}

# Early entry tiers in seconds -> score (PRD Section 8.3)
EARLY_ENTRY_TIERS = [
    (120, 100.0),    # 0 - 2 min
    (300, 90.0),     # >2 - 5 min
    (600, 75.0),     # >5 - 10 min
    (1200, 55.0),    # >10 - 20 min
    (3600, 30.0),    # >20 - 60 min
    (float("inf"), 10.0), # >60 min
]


@dataclass
class Settings:
    """Application settings with environment variable fallbacks."""

    solscan_api_key: str = field(
        default_factory=lambda: os.getenv("SOLSCAN_API_KEY", "")
    )
    sqlite_db_path: Path = field(
        default_factory=lambda: Path(os.getenv("SQLITE_DB_PATH", "early_buyer.db"))
    )
    request_timeout: float = field(
        default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT", "30.0"))
    )
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("MAX_RETRIES", "5"))
    )
    base_url: str = field(
        default_factory=lambda: os.getenv("SOLSCAN_BASE_URL", "https://pro-api.solscan.io/v2.0")
    )
    rate_limit_rps: float = field(
        default_factory=lambda: float(os.getenv("RATE_LIMIT_RPS", "5.0"))
    )
    wsol_mint: str = WSOL_MINT
    scoring_weights: Dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SCORING_WEIGHTS)
    )


# Global settings singleton
settings = Settings()
