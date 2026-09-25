"""Solscan Pro API client with SQLite caching, rate limiting, and exponential backoff.

Follows the Cache-First principle: queries SQLite disk cache prior to any
external network call. Employs jittered exponential backoff for HTTP 429 & 5xx.
"""

import logging
import random
import time
from typing import Any, Dict, Optional
import httpx

from config import settings
from storage.database import Database, compute_cache_key

logger = logging.getLogger(__name__)


class SolscanAPIError(Exception):
    """Exception raised for unrecoverable errors from the Solscan Pro API."""

    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class SolscanClient:
    """Resilient Solscan Pro API client with integrated SQLite caching."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        rate_limit_rps: Optional[float] = None,
        database: Optional[Database] = None,
    ):
        self.api_key = api_key if api_key is not None else settings.solscan_api_key
        self.base_url = (base_url or settings.base_url).rstrip("/")
        self.timeout = timeout or settings.request_timeout
        self.max_retries = max_retries or settings.max_retries
        self.rate_limit_rps = rate_limit_rps or settings.rate_limit_rps
        self.min_interval = 1.0 / self.rate_limit_rps if self.rate_limit_rps > 0 else 0.0

        self.db = database or Database(settings.sqlite_db_path)
        self._last_request_time = 0.0

        # Build HTTP client
        headers = {
            "token": self.api_key,
            "Accept": "application/json",
            "User-Agent": "EarlyBuyerScanner/1.0",
        }
        self.client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(self.timeout),
        )

    def _throttle(self) -> None:
        """Enforce request rate limit before dispatching a network call."""
        if self.min_interval <= 0:
            return
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request_time = time.time()

    def _request_with_retry(
        self, endpoint: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute network request with jittered exponential backoff for 429 and 5xx."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        base_backoff = 1.0
        max_backoff = 60.0

        for attempt in range(self.max_retries):
            self._throttle()
            try:
                response = self.client.get(url, params=params)

                # Successful response
                if response.status_code == 200:
                    return response.json()

                # Rate limited (429) or Server error (5xx) -> Backoff & Retry
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after_hdr = response.headers.get("Retry-After")
                    if retry_after_hdr:
                        try:
                            wait_time = float(retry_after_hdr)
                        except ValueError:
                            wait_time = min(max_backoff, base_backoff * (2 ** attempt) + random.uniform(0.1, 0.5))
                    else:
                        wait_time = min(max_backoff, base_backoff * (2 ** attempt) + random.uniform(0.1, 0.5))

                    logger.warning(
                        f"Solscan API returned HTTP {response.status_code} for {endpoint}. "
                        f"Retrying in {wait_time:.2f}s (Attempt {attempt + 1}/{self.max_retries})."
                    )

                    if attempt == self.max_retries - 1:
                        raise SolscanAPIError(
                            f"Max retries ({self.max_retries}) exceeded for {endpoint}. "
                            f"Last status: {response.status_code}",
                            status_code=response.status_code,
                            response_text=response.text,
                        )

                    time.sleep(wait_time)
                    continue

                # Non-retryable client errors (400, 401, 403, 404, etc.)
                raise SolscanAPIError(
                    f"Solscan API error {response.status_code} on {endpoint}: {response.text}",
                    status_code=response.status_code,
                    response_text=response.text,
                )

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                wait_time = min(max_backoff, base_backoff * (2 ** attempt) + random.uniform(0.1, 0.5))
                logger.warning(
                    f"Network error on {endpoint}: {exc}. "
                    f"Retrying in {wait_time:.2f}s (Attempt {attempt + 1}/{self.max_retries})."
                )
                if attempt == self.max_retries - 1:
                    raise SolscanAPIError(
                        f"Max retries reached on connection error: {exc}"
                    ) from exc
                time.sleep(wait_time)

        raise SolscanAPIError(f"Failed to fetch {endpoint} after {self.max_retries} attempts.")

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Retrieve data adhering to Cache-First: Cache -> Network -> Cache Store."""
        cache_key = compute_cache_key(endpoint, params)

        # 1. Check local SQLite cache first
        cached = self.db.get_cached_response(cache_key)
        if cached is not None:
            logger.debug(f"Cache hit for {endpoint} with key {cache_key}")
            return cached

        # 2. Cache miss: Fetch from Solscan Pro API
        data = self._request_with_retry(endpoint, params)

        # 3. Store response into SQLite disk cache
        self.db.set_cached_response(cache_key, endpoint, params, data)
        return data

    # ==========================================
    # ENDPOINT WRAPPERS
    # ==========================================

    def get_token_meta(self, token_address: str) -> Dict[str, Any]:
        """Fetch token metadata (/token/meta?address=...)."""
        return self.get("token/meta", {"address": token_address})

    def get_token_transfers(
        self,
        token_address: str,
        page: int = 1,
        page_size: int = 100,
        sort_by: str = "block_time",
        sort_order: str = "asc",
    ) -> Dict[str, Any]:
        """Fetch historical token transfers (/token/transfer)."""
        params = {
            "address": token_address,
            "page": page,
            "page_size": page_size,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        return self.get("token/transfer", params)

    def get_playground_token_transfers(
        self,
        token_address: str,
        page: int = 1,
        page_size: int = 10,
        sort_by: str = "block_time",
        sort_order: str = "asc",
    ) -> Dict[str, Any]:
        """Fetch historical token transfers from Solscan Playground endpoint (/playground/token/transfer).
        
        Strictly for FREE_LIMITED mode within local safety budgets.
        Follows cache-first with SQLite and handles 429/401/400 without aggressive/infinite retries.
        """
        params = {
            "address": token_address,
            "page": page,
            "page_size": page_size,
            "sort_by": sort_by,
            "sort_order": sort_order,
        }
        endpoint = "playground/token/transfer"
        cache_key = compute_cache_key(endpoint, params)

        # 1. Check local SQLite cache first
        cached = self.db.get_cached_response(cache_key)
        if cached is not None:
            logger.debug(f"Cache hit for {endpoint} with key {cache_key}")
            return cached

        # 2. Cache miss: Fetch from Solscan Playground
        playground_base = getattr(settings, "free_playground_base_url", "https://pro-api.solscan.io/playground").rstrip("/")
        url = f"{playground_base}/token/transfer"

        self._throttle()
        try:
            response = self.client.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                self.db.set_cached_response(cache_key, endpoint, params, data)
                return data
            elif response.status_code == 429:
                logger.warning("Solscan Playground rate limited (HTTP 429). Halting playground requests.")
                raise SolscanAPIError("Solscan Playground rate limited (HTTP 429)", status_code=429, response_text=response.text)
            elif response.status_code == 401:
                logger.warning("Solscan Playground authentication rejected (HTTP 401).")
                raise SolscanAPIError("Solscan Playground unauthorized (HTTP 401)", status_code=401, response_text=response.text)
            elif response.status_code == 400:
                logger.warning(f"Solscan Playground validation error (HTTP 400): {response.text}")
                raise SolscanAPIError(f"Solscan Playground validation error (HTTP 400): {response.text}", status_code=400, response_text=response.text)
            else:
                logger.warning(f"Solscan Playground error HTTP {response.status_code}: {response.text}")
                raise SolscanAPIError(f"Solscan Playground error HTTP {response.status_code}", status_code=response.status_code, response_text=response.text)
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            logger.warning(f"Network error on Solscan Playground: {exc}")
            raise SolscanAPIError(f"Network error on Solscan Playground: {exc}") from exc

    def get_token_holders(
        self,
        token_address: str,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """Fetch top token holders (/token/holders)."""
        params = {
            "address": token_address,
            "page": page,
            "page_size": page_size,
        }
        return self.get("token/holders", params)

    def get_transaction_detail(self, signature: str) -> Dict[str, Any]:
        """Fetch on-chain transaction details (/transaction/detail?tx=...)."""
        return self.get("transaction/detail", {"tx": signature})

    def close(self) -> None:
        """Close HTTP client session."""
        self.client.close()

    def __enter__(self) -> "SolscanClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
