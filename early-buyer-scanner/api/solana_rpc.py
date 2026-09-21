"""Solana Native JSON-RPC 2.0 client for Early Buyer Scanner (EBRS).

Eliminates dependency on third-party paid APIs by querying the Solana blockchain directly.
Supports Helius / Public RPC with SQLite caching (Cache-First pattern),
rate limiting, and exponential backoff for HTTP 429 and RPC rate limits.
"""

import json
import logging
import random
import time
from typing import Any, Dict, List, Optional
import httpx

from config import settings
from storage.database import Database, compute_cache_key

logger = logging.getLogger(__name__)


class SolanaRPCError(Exception):
    """Exception raised for Solana RPC errors."""

    def __init__(self, message: str, code: Optional[int] = None, data: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.data = data


class SolanaRpcClient:
    """Client for querying the Solana JSON-RPC 2.0 API with SQLite caching."""

    def __init__(
        self,
        rpc_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        rate_limit_rps: Optional[float] = None,
        database: Optional[Database] = None,
    ):
        self.rpc_url = rpc_url or settings.solana_rpc_url
        self.timeout = timeout or settings.request_timeout
        self.max_retries = max_retries or settings.max_retries
        self.rate_limit_rps = rate_limit_rps or settings.rate_limit_rps
        self.min_interval = 1.0 / self.rate_limit_rps if self.rate_limit_rps > 0 else 0.0

        self.db = database or Database(settings.sqlite_db_path)
        self._last_request_time = 0.0
        self._req_id = 1

        self.client = httpx.Client(
            timeout=httpx.Timeout(self.timeout),
            headers={"Content-Type": "application/json", "User-Agent": "EBRS-SolanaRPC/1.0"},
        )

    def _throttle(self) -> None:
        """Enforce request rate limit interval."""
        if self.min_interval <= 0:
            return
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_time = time.time()

    def _call_rpc(self, method: str, params: Optional[List[Any]] = None) -> Any:
        """Execute JSON-RPC call with Cache-First SQLite lookup and exponential backoff."""
        params = params or []
        cache_key = compute_cache_key(f"rpc:{method}", {"params": params})

        # 1. Check local SQLite cache
        cached = self.db.get_cached_response(cache_key)
        if cached is not None:
            logger.debug(f"RPC Cache hit for {method} [{cache_key[:8]}]")
            return cached

        # 2. Dispatch network request with retries
        base_backoff = 1.0
        max_backoff = 30.0

        payload = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params,
        }
        self._req_id += 1

        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.client.post(self.rpc_url, json=payload)

                # HTTP-level rate limit or server error
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait_time = min(max_backoff, base_backoff * (2 ** attempt) + random.uniform(0.1, 0.5))
                    logger.warning(
                        f"RPC returned HTTP {resp.status_code} for {method}. "
                        f"Retrying in {wait_time:.2f}s ({attempt + 1}/{self.max_retries})..."
                    )
                    time.sleep(wait_time)
                    continue

                if resp.status_code != 200:
                    raise SolanaRPCError(
                        f"HTTP {resp.status_code} error from RPC endpoint: {resp.text}",
                        code=resp.status_code,
                    )

                data = resp.json()

                # JSON-RPC error response
                if "error" in data and data["error"]:
                    err = data["error"]
                    err_code = err.get("code")
                    err_msg = err.get("message", "")

                    # Error -32005 is standard Solana RPC rate limit exceeded
                    if err_code == -32005 or "rate limit" in err_msg.lower():
                        wait_time = min(max_backoff, base_backoff * (2 ** attempt) + random.uniform(0.1, 0.5))
                        logger.warning(
                            f"Solana RPC Rate Limit (-32005) for {method}. "
                            f"Retrying in {wait_time:.2f}s ({attempt + 1}/{self.max_retries})..."
                        )
                        time.sleep(wait_time)
                        continue

                    raise SolanaRPCError(
                        f"Solana RPC Error for {method}: {err_msg} (code {err_code})",
                        code=err_code,
                        data=err.get("data"),
                    )

                result = data.get("result")

                # 3. Store in SQLite cache
                self.db.set_cached_response(cache_key, f"rpc:{method}", {"params": params}, result)
                return result

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                wait_time = min(max_backoff, base_backoff * (2 ** attempt) + random.uniform(0.1, 0.5))
                logger.warning(
                    f"Network error on {method}: {exc}. "
                    f"Retrying in {wait_time:.2f}s ({attempt + 1}/{self.max_retries})..."
                )
                if attempt == self.max_retries - 1:
                    raise SolanaRPCError(f"RPC connection failure after {self.max_retries} attempts: {exc}") from exc
                time.sleep(wait_time)

        raise SolanaRPCError(f"Max retries exceeded for RPC call {method}")

    # ==========================================
    # CORE RPC METHODS
    # ==========================================

    def get_signatures_for_address(
        self,
        account_address: str,
        limit: int = 100,
        before: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get confirmed signatures for an account (newest to oldest).
        
        Args:
            account_address: Public key to query.
            limit: Maximum signatures (1-1000).
            before: Start searching backwards from this signature.
            until: Search until this signature.
        """
        config: Dict[str, Any] = {"limit": limit}
        if before:
            config["before"] = before
        if until:
            config["until"] = until

        result = self._call_rpc("getSignaturesForAddress", [account_address, config])
        return result if isinstance(result, list) else []

    def get_parsed_transaction(self, signature: str) -> Optional[Dict[str, Any]]:
        """Get detailed parsed transaction by signature."""
        config = {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 1,
        }
        try:
            return self._call_rpc("getTransaction", [signature, config])
        except SolanaRPCError as exc:
            if exc.code == -32015:
                # Fallback to version 0 if a node only supports up to 0
                config["maxSupportedTransactionVersion"] = 0
                return self._call_rpc("getTransaction", [signature, config])
            raise

    def get_token_largest_accounts(self, mint_address: str) -> List[Dict[str, Any]]:
        """Get the 20 largest token accounts for a token mint."""
        result = self._call_rpc("getTokenLargestAccounts", [mint_address])
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result if isinstance(result, list) else []

    def get_token_supply(self, mint_address: str) -> Dict[str, Any]:
        """Get total token supply and decimals for a token mint."""
        result = self._call_rpc("getTokenSupply", [mint_address])
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result or {}

    def get_account_info(self, account_address: str, encoding: str = "jsonParsed") -> Optional[Dict[str, Any]]:
        """Get account info with specified encoding (jsonParsed, base64, etc.)."""
        config = {"encoding": encoding}
        result = self._call_rpc("getAccountInfo", [account_address, config])
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result

    def get_multiple_accounts(self, account_addresses: List[str]) -> List[Optional[Dict[str, Any]]]:
        """Get multiple parsed accounts in a single batch RPC request."""
        if not account_addresses:
            return []
        config = {"encoding": "jsonParsed"}
        result = self._call_rpc("getMultipleAccounts", [account_addresses, config])
        if isinstance(result, dict) and "value" in result:
            return result["value"]
        return result if isinstance(result, list) else []

    # ==========================================
    # STANDARDIZED ADAPTER METHOD
    # ==========================================

    def get_transaction_detail(self, signature: str) -> Dict[str, Any]:
        """Fetch on-chain transaction and format into standardized Solscan-compatible schema.
        
        This enables 100% interoperability with existing downstream classifiers and models.
        """
        tx_data = self.get_parsed_transaction(signature)
        if not tx_data:
            return {
                "success": False,
                "data": {
                    "tx_hash": signature,
                    "status": "Failed",
                    "block_time": 0,
                    "signer": [],
                    "sol_bal_change": [],
                    "token_bal_change": [],
                    "programs_involved": [],
                },
            }

        meta = tx_data.get("meta", {})
        transaction = tx_data.get("transaction", {})
        message = transaction.get("message", {})
        account_keys = message.get("accountKeys", [])

        # Extract primary signer
        signers = []
        for key_info in account_keys:
            if isinstance(key_info, dict) and key_info.get("signer"):
                signers.append(key_info.get("pubkey", ""))
            elif isinstance(key_info, str):
                signers.append(key_info)
        primary_signer = signers[0] if signers else ""

        block_time = tx_data.get("blockTime") or 0
        slot = tx_data.get("slot")

        # Calculate SOL balance changes
        pre_balances = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])
        sol_changes = []
        for idx, key_info in enumerate(account_keys):
            pubkey = key_info.get("pubkey") if isinstance(key_info, dict) else str(key_info)
            pre = pre_balances[idx] if idx < len(pre_balances) else 0
            post = post_balances[idx] if idx < len(post_balances) else 0
            diff = post - pre
            if diff != 0:
                sol_changes.append({
                    "address": pubkey,
                    "pre_balance": pre,
                    "post_balance": post,
                    "change": diff,
                })

        # Calculate Token balance changes
        pre_tokens = {
            f"{b.get('owner')}_{b.get('mint')}": b
            for b in meta.get("preTokenBalances", [])
        }
        post_tokens = {
            f"{b.get('owner')}_{b.get('mint')}": b
            for b in meta.get("postTokenBalances", [])
        }

        token_changes = []
        all_keys = set(pre_tokens.keys()) | set(post_tokens.keys())
        for k in all_keys:
            pr = pre_tokens.get(k, {})
            po = post_tokens.get(k, {})
            owner = po.get("owner") or pr.get("owner") or ""
            mint = po.get("mint") or pr.get("mint") or ""
            ui_pre = pr.get("uiTokenAmount", {}).get("uiAmount")
            ui_post = po.get("uiTokenAmount", {}).get("uiAmount")
            pre_amt = float(ui_pre) if ui_pre is not None else 0.0
            post_amt = float(ui_post) if ui_post is not None else 0.0
            diff = post_amt - pre_amt
            decs = (
                po.get("uiTokenAmount", {}).get("decimals")
                or pr.get("uiTokenAmount", {}).get("decimals")
                or 9
            )
            if diff != 0:
                token_changes.append({
                    "address": owner,
                    "token_address": mint,
                    "pre_balance": pre_amt,
                    "post_balance": post_amt,
                    "change": diff,
                    "decimals": decs,
                })

        # Extract all programs involved
        programs = []
        for ix in message.get("instructions", []):
            if isinstance(ix, dict) and "programId" in ix:
                programs.append(ix["programId"])
        for inner in meta.get("innerInstructions", []):
            for ix in inner.get("instructions", []):
                if isinstance(ix, dict) and "programId" in ix:
                    programs.append(ix["programId"])
        unique_programs = list(dict.fromkeys(programs))

        return {
            "success": True,
            "data": {
                "tx_hash": signature,
                "block_time": block_time,
                "slot": slot,
                "status": 1 if meta.get("err") is None else 0,
                "fee": meta.get("fee", 5000),
                "signer": [primary_signer],
                "sol_bal_change": sol_changes,
                "token_bal_change": token_changes,
                "programs_involved": unique_programs,
            },
        }

    def close(self) -> None:
        """Close underlying HTTP client."""
        self.client.close()

    def __enter__(self) -> "SolanaRpcClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
