"""SQLite Database storage and API response caching for EBRS.

Enforces WAL mode, executes PRD Section 11 schemas, provides
deterministic SHA-256 caching for Solscan API, and atomic transaction management.
"""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Generator, List, Optional, Union

from models.schemas import (
    ConfidenceEnum,
    TokenMetadata,
    TransactionClassification,
    WalletProfile,
)


def compute_cache_key(endpoint: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Compute a deterministic SHA-256 cache key from endpoint and sorted query params."""
    normalized_endpoint = endpoint.strip().lower()
    # Ensure params are sorted deterministically
    sorted_params_str = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    key_input = f"{normalized_endpoint}?{sorted_params_str}"
    return hashlib.sha256(key_input.encode("utf-8")).hexdigest()


class Database:
    """SQLite Database manager with WAL mode and atomic transaction management."""

    def __init__(self, db_path: Union[str, Path] = "early_buyer.db"):
        self.db_path = str(db_path)
        self._is_memory = self.db_path == ":memory:"
        self._memory_conn: Optional[sqlite3.Connection] = None

        if self._is_memory:
            self._memory_conn = sqlite3.connect(
                ":memory:",
                check_same_thread=False,
            )
            self._memory_conn.row_factory = sqlite3.Row

        self.init_schema()

    def get_connection(self) -> sqlite3.Connection:
        """Get an open SQLite connection."""
        if self._is_memory and self._memory_conn is not None:
            return self._memory_conn

        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for high concurrency disk persistence
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for atomic database transactions.
        
        Commits on normal exit, rolls back on any exception.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE;")
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            if not self._is_memory:
                conn.close()

    def init_schema(self) -> None:
        """Initialize all required tables and indices according to PRD Section 11."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # Table 1: tokens (PRD 11)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_address TEXT UNIQUE NOT NULL,
                    name TEXT,
                    symbol TEXT,
                    decimals INTEGER DEFAULT 9,
                    creator TEXT,
                    launch_time INTEGER,
                    launch_confidence TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Table 2: transactions (PRD 11)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signature TEXT UNIQUE NOT NULL,
                    token_address TEXT NOT NULL,
                    block_time INTEGER NOT NULL,
                    wallet TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    sol_change REAL DEFAULT 0.0,
                    token_change REAL DEFAULT 0.0,
                    programs TEXT,
                    raw_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Table 3: wallet_profiles (PRD 11)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS wallet_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_address TEXT NOT NULL,
                    token_address TEXT NOT NULL,
                    first_buy_time INTEGER,
                    first_buy_signature TEXT,
                    first_buy_amount REAL DEFAULT 0.0,
                    time_after_launch INTEGER,
                    total_buy_amount REAL DEFAULT 0.0,
                    buy_count INTEGER DEFAULT 0,
                    sell_count INTEGER DEFAULT 0,
                    total_sell_amount REAL DEFAULT 0.0,
                    current_holding REAL DEFAULT 0.0,
                    exit_ratio REAL DEFAULT 0.0,
                    holder_rank INTEGER,
                    holder_percentage REAL,
                    score REAL DEFAULT 0.0,
                    confidence TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (wallet_address, token_address)
                );
                """
            )

            # Table 4: wallet_events (PRD 11)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS wallet_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    wallet_address TEXT NOT NULL,
                    token_address TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    amount REAL DEFAULT 0.0,
                    quote_amount REAL DEFAULT 0.0,
                    confidence TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Table 5: api_cache (Disk persistence for Solscan Pro calls)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    endpoint TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

            # Performance Indices
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_sig ON transactions (signature);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_wallet ON transactions (wallet, token_address);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_profiles_score ON wallet_profiles (token_address, score DESC);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_events_wallet ON wallet_events (wallet_address, token_address);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_api_cache_endpoint ON api_cache (endpoint);"
            )

            conn.commit()
        finally:
            cursor.close()
            if not self._is_memory:
                conn.close()

    # ==========================================
    # API CACHE OPERATIONS
    # ==========================================

    def get_cached_response(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached JSON response by cache_key."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT response_json FROM api_cache WHERE cache_key = ?;",
                (cache_key,),
            )
            row = cursor.fetchone()
            if row:
                return json.loads(row["response_json"])
            return None
        finally:
            cursor.close()
            if not self._is_memory:
                conn.close()

    def set_cached_response(
        self,
        cache_key: str,
        endpoint: str,
        params: Optional[Dict[str, Any]],
        response_data: Any,
    ) -> None:
        """Store API response JSON in api_cache."""
        params_str = json.dumps(params or {}, sort_keys=True)
        response_str = json.dumps(response_data)

        with self.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO api_cache (cache_key, endpoint, params_json, response_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    response_json = excluded.response_json,
                    created_at = CURRENT_TIMESTAMP;
                """,
                (cache_key, endpoint, params_str, response_str),
            )

    # ==========================================
    # TOKEN METADATA OPERATIONS
    # ==========================================

    def save_token(self, token: TokenMetadata) -> None:
        """Insert or update token metadata."""
        confidence_val = (
            token.launch_confidence.value
            if isinstance(token.launch_confidence, ConfidenceEnum)
            else str(token.launch_confidence)
        )
        with self.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO tokens (token_address, name, symbol, decimals, creator, launch_time, launch_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_address) DO UPDATE SET
                    name = excluded.name,
                    symbol = excluded.symbol,
                    decimals = excluded.decimals,
                    creator = excluded.creator,
                    launch_time = excluded.launch_time,
                    launch_confidence = excluded.launch_confidence;
                """,
                (
                    token.token_address,
                    token.name,
                    token.symbol,
                    token.decimals,
                    token.creator,
                    token.launch_time,
                    confidence_val,
                ),
            )

    def get_token(self, token_address: str) -> Optional[TokenMetadata]:
        """Fetch token metadata by token address."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT token_address, name, symbol, decimals, creator, launch_time, launch_confidence, created_at "
                "FROM tokens WHERE token_address = ?;",
                (token_address,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return TokenMetadata(
                token_address=row["token_address"],
                name=row["name"],
                symbol=row["symbol"],
                decimals=row["decimals"],
                creator=row["creator"],
                launch_time=row["launch_time"],
                launch_confidence=ConfidenceEnum(row["launch_confidence"])
                if row["launch_confidence"]
                else ConfidenceEnum.LOW,
                created_at=str(row["created_at"]),
            )
        finally:
            cursor.close()
            if not self._is_memory:
                conn.close()

    # ==========================================
    # TRANSACTION OPERATIONS
    # ==========================================

    def has_transaction(self, signature: str) -> bool:
        """Check whether a transaction signature already exists in the database."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT 1 FROM transactions WHERE signature = ? LIMIT 1;",
                (signature,),
            )
            return cursor.fetchone() is not None
        finally:
            cursor.close()
            if not self._is_memory:
                conn.close()

    def save_transaction(
        self,
        tx: TransactionClassification,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert or update a classified transaction."""
        programs_json = json.dumps(tx.programs)
        raw_data_json = json.dumps(raw_data) if raw_data is not None else None

        with self.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO transactions (
                    signature, token_address, block_time, wallet,
                    classification, confidence, sol_change, token_change,
                    programs, raw_data
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signature) DO UPDATE SET
                    classification = excluded.classification,
                    confidence = excluded.confidence,
                    sol_change = excluded.sol_change,
                    token_change = excluded.token_change,
                    programs = excluded.programs,
                    raw_data = COALESCE(excluded.raw_data, transactions.raw_data);
                """,
                (
                    tx.signature,
                    tx.token_address,
                    tx.block_time,
                    tx.wallet,
                    tx.classification.value,
                    tx.confidence.value,
                    tx.sol_change,
                    tx.token_change,
                    programs_json,
                    raw_data_json,
                ),
            )

    def get_transaction(self, signature: str) -> Optional[Dict[str, Any]]:
        """Retrieve stored transaction record by signature."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT * FROM transactions WHERE signature = ?;",
                (signature,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            res = dict(row)
            if res.get("programs"):
                res["programs"] = json.loads(res["programs"])
            if res.get("raw_data"):
                res["raw_data"] = json.loads(res["raw_data"])
            return res
        finally:
            cursor.close()
            if not self._is_memory:
                conn.close()

    # ==========================================
    # WALLET PROFILE OPERATIONS
    # ==========================================

    def save_wallet_profile(self, profile: WalletProfile) -> None:
        """Insert or update a wallet profile."""
        with self.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO wallet_profiles (
                    wallet_address, token_address, first_buy_time, first_buy_signature,
                    first_buy_amount, time_after_launch, total_buy_amount, buy_count,
                    sell_count, total_sell_amount, current_holding, exit_ratio,
                    holder_rank, holder_percentage, score, confidence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address, token_address) DO UPDATE SET
                    first_buy_time = excluded.first_buy_time,
                    first_buy_signature = excluded.first_buy_signature,
                    first_buy_amount = excluded.first_buy_amount,
                    time_after_launch = excluded.time_after_launch,
                    total_buy_amount = excluded.total_buy_amount,
                    buy_count = excluded.buy_count,
                    sell_count = excluded.sell_count,
                    total_sell_amount = excluded.total_sell_amount,
                    current_holding = excluded.current_holding,
                    exit_ratio = excluded.exit_ratio,
                    holder_rank = excluded.holder_rank,
                    holder_percentage = excluded.holder_percentage,
                    score = excluded.score,
                    confidence = excluded.confidence;
                """,
                (
                    profile.wallet_address,
                    profile.token_address,
                    profile.first_buy_time,
                    profile.first_buy_signature,
                    profile.first_buy_amount,
                    profile.time_after_launch,
                    profile.total_buy_amount,
                    profile.buy_count,
                    profile.sell_count,
                    profile.total_sell_amount,
                    profile.current_holding,
                    profile.exit_ratio,
                    profile.holder_rank,
                    profile.holder_percentage,
                    profile.score,
                    profile.confidence.value,
                ),
            )

    def get_wallet_profiles(self, token_address: str) -> List[WalletProfile]:
        """Fetch all wallet profiles for a given token sorted by score descending."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT * FROM wallet_profiles WHERE token_address = ? ORDER BY score DESC;",
                (token_address,),
            )
            rows = cursor.fetchall()
            profiles = []
            for r in rows:
                profiles.append(
                    WalletProfile(
                        wallet_address=r["wallet_address"],
                        token_address=r["token_address"],
                        first_buy_time=r["first_buy_time"],
                        first_buy_signature=r["first_buy_signature"],
                        first_buy_amount=r["first_buy_amount"],
                        time_after_launch=r["time_after_launch"],
                        total_buy_amount=r["total_buy_amount"],
                        buy_count=r["buy_count"],
                        sell_count=r["sell_count"],
                        total_sell_amount=r["total_sell_amount"],
                        current_holding=r["current_holding"],
                        exit_ratio=r["exit_ratio"],
                        holder_rank=r["holder_rank"],
                        holder_percentage=r["holder_percentage"],
                        score=r["score"],
                        confidence=ConfidenceEnum(r["confidence"])
                        if r["confidence"]
                        else ConfidenceEnum.UNKNOWN,
                    )
                )
            return profiles
        finally:
            cursor.close()
            if not self._is_memory:
                conn.close()

    # ==========================================
    # WALLET EVENTS OPERATIONS
    # ==========================================

    def save_wallet_event(
        self,
        wallet_address: str,
        token_address: str,
        signature: str,
        timestamp: int,
        event_type: str,
        amount: float,
        quote_amount: float = 0.0,
        confidence: str = "HIGH",
    ) -> None:
        """Record an on-chain event associated with a wallet."""
        with self.transaction() as cursor:
            cursor.execute(
                """
                INSERT INTO wallet_events (
                    wallet_address, token_address, signature,
                    timestamp, event_type, amount, quote_amount, confidence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    wallet_address,
                    token_address,
                    signature,
                    timestamp,
                    event_type,
                    amount,
                    quote_amount,
                    confidence,
                ),
            )

    def get_wallet_events(
        self, wallet_address: str, token_address: str
    ) -> List[Dict[str, Any]]:
        """Retrieve all events for a wallet and token sorted chronologically."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT * FROM wallet_events "
                "WHERE wallet_address = ? AND token_address = ? "
                "ORDER BY timestamp ASC;",
                (wallet_address, token_address),
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            cursor.close()
            if not self._is_memory:
                conn.close()

    def close(self) -> None:
        """Close memory connection if active."""
        if self._is_memory and self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None
