"""Configuration module for Early Buyer Scanner (EBRS).

Handles environment variables, Solana constants, DEX program IDs,
and default scoring weights as defined in PRD Section 8.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Dict, Set
from dotenv import load_dotenv

# Load .env if present (check module directory first, then cwd)
ENV_FILE = Path(__file__).resolve().parent / ".env"
if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE)
else:
    load_dotenv()

# ==========================================
# SOLANA ADDRESS CONSTANTS
# ==========================================

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# Known Quote Assets (WSOL, USDC, USDT)
QUOTE_ASSET_MINTS: Set[str] = {
    WSOL_MINT,
    USDC_MINT,
    USDT_MINT,
}

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

# Pump.fun Ecosystem AMM & Fee Programs
PUMP_SWAP_AMM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FEES_PROGRAM_ID = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"

# High-volume Solana Trading & Sniper Routers
AXIOM_TRADE_ROUTER_ID = "FLASHX8DrLbgeR8FcfNV1F5krxYcYMUdBkrP1EPBtxB9"

# Set of DEX / AMM Programs for fast lookup during classification
KNOWN_DEX_PROGRAMS: Set[str] = {
    PUMP_FUN_PROGRAM_ID,
    PUMP_FUN_FEE_MIGRATION_ID,
    PUMP_SWAP_AMM_ID,
    PUMP_FEES_PROGRAM_ID,
    AXIOM_TRADE_ROUTER_ID,
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

# Known Burn and Null addresses
BURN_ADDRESSES: Set[str] = {
    "11111111111111111111111111111111",
    "deaddeaddeaddeaddeaddeaddeaddeaddeaddeaddead",
    "1111111111111111111111111111111111111111111",
    "00000000000000000000000000000000000000000000",
}

# ==========================================
# KNOWN CEX HOT WALLETS (Solana Mainnet)
# ==========================================

KNOWN_CEX_WALLETS: Dict[str, str] = {
    # Binance
    "5tzFkiKscMRHK5ZXkrZXZ1RChPTyVC5yFsNuPaSkWCjd": "Binance",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance",
    # Coinbase
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase",
    "2AQdpHJ2JpcEgBtAZUXpqkWwdDTdy53T5YMQU45y4qHT": "Coinbase",
    "GJRs4FwHtemZ5ZE9x3FNvJ83nPwm5kWsfGQ65sJUCwa7": "Coinbase",
    # OKX
    "5VCwKtCXgCJ6kit5FybXjvriW3xJMsFD897fDrx4wh9B": "OKX",
    "6ZRCB7AAqGrepmKBZnnxrwifrWsKj5Kx4Pq7oZqZeaZ": "OKX",
    # Bybit
    "AC5RDfQFmDS1deWZos921qbhirGLTgFgSLmjmbBtx2x": "Bybit",
    # MEXC
    "ASTyfSima4LLAdEmpgA7ddJB69YKA2eE64YQCDPie2u": "MEXC",
    # KuCoin
    "BMbLwH2vE4FjQkY8c2B5oGqP2vY1nC6fK8X3vY1nC6f": "KuCoin",
    # FixedFloat
    "FCgXvN8H9a9cT2rUqJpXm3Kz7L9oQ1vW4yB8xZ5nE2r": "FixedFloat",
    # Kraken
    "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken",
    # Gate.io
    "u6PJ8DtQuqaeagbdrK9ceA2SZ42eeP5UV2U4ErqmNNN": "Gate.io",
}

# ==========================================
# FUNDING & SYBIL DETECTION CONSTANTS
# ==========================================

MAX_FUNDING_PAGES: int = 3
MATURE_WALLET_AGE_DAYS: float = 7.0
MIN_CLUSTER_FUNDING_SOL: float = 0.05

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


PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve_db_path() -> Path:
    raw = os.getenv("SQLITE_DB_PATH", "early_buyer.db")
    if raw == ":memory:":
        return Path(":memory:")
    p = Path(raw)
    if not p.is_absolute():
        return PROJECT_ROOT / p
    return p


def _resolve_rpc_url() -> str:
    custom_url = os.getenv("SOLANA_RPC_URL", "").strip()
    if custom_url:
        return custom_url
    helius_key = os.getenv("HELIUS_API_KEY", "").strip()
    if helius_key:
        return f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
    return "https://api.mainnet-beta.solana.com"


@dataclass
class Settings:
    """Application settings with environment variable fallbacks."""

    solana_rpc_url: str = field(default_factory=_resolve_rpc_url)
    helius_api_key: str = field(default_factory=lambda: os.getenv("HELIUS_API_KEY", ""))
    solscan_api_key: str = field(
        default_factory=lambda: os.getenv("SOLSCAN_API_KEY", "")
    )
    sqlite_db_path: Path = field(
        default_factory=_resolve_db_path
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
