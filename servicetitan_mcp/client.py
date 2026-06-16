"""Low-level HTTP client for ServiceTitan API v2.

Features:
  - Shared httpx.AsyncClient (connection pooling across tool calls).
  - Token-bucket rate limiting with a separate bucket for the reporting API
    (ServiceTitan rate-limits reporting much more aggressively: 5 req/min vs
    60 req/sec on the main API).
  - Concurrency semaphore to cap in-flight requests regardless of how many
    tools the LLM fires in parallel.
  - Retry with exponential backoff on 429/502/503/504. Honors Retry-After.

Environment variables (read at module load):
  ST_RATE_LIMIT_RPS  — main API ceiling in req/sec (default 30; ST's hard cap
                       is 60 so we run at half to absorb bursts).
  ST_REPORTING_RPM   — reporting API ceiling in req/min (default 3; ST's hard
                       cap is 5).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field

import httpx

from .auth import token_manager

API_BASE = "https://api.servicetitan.io"
RETRY_STATUS = frozenset({429, 502, 503, 504})


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


MAIN_RPS = _env_float("ST_RATE_LIMIT_RPS", 30.0)
REPORTING_RPM = _env_float("ST_REPORTING_RPM", 3.0)
DEFAULT_CONCURRENCY = int(_env_float("ST_MAX_CONCURRENCY", 10))


class TokenBucket:
    """Async token bucket. `rate` tokens refill per second, up to `capacity`."""

    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                needed = cost - self._tokens
                wait = needed / self.rate
                await asyncio.sleep(wait)


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_backoff: float = 1.0
    max_backoff: float = 30.0
    retry_status: frozenset = field(default_factory=lambda: RETRY_STATUS)


def _raise_with_body(resp: httpx.Response) -> None:
    """Raise HTTPStatusError with response body included for easier debugging."""
    if resp.is_error:
        body = resp.text[:2000]
        raise httpx.HTTPStatusError(
            f"{resp.status_code} {resp.reason_phrase} for {resp.request.url}\n"
            f"Response body: {body}",
            request=resp.request,
            response=resp,
        )


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """Parse Retry-After header. Returns seconds as float, or None."""
    header = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if not header:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        # HTTP-date form — rare in practice for ST; fall through to backoff.
        return None


def _log(msg: str) -> None:
    print(f"[servicetitan-mcp] {msg}", file=sys.stderr, flush=True)


# Module-level shared AsyncClient, lazily created.
_shared_client: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=60,
            limits=httpx.Limits(
                max_connections=DEFAULT_CONCURRENCY * 2,
                max_keepalive_connections=DEFAULT_CONCURRENCY,
            ),
        )
    return _shared_client


async def aclose() -> None:
    """Close the shared client. Call on graceful shutdown."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None


# Per-tenant rate limiters. ServiceTitan's quotas are per-app-per-tenant, so
# buckets must not be shared across tenants (that would under-utilize by N×).
# Concurrency semaphore stays process-wide — it only guards local httpx fan-out,
# not ST quota.
_main_limiters: dict[str, TokenBucket] = {}
_reporting_limiters: dict[str, TokenBucket] = {}
_concurrency_sem = asyncio.Semaphore(DEFAULT_CONCURRENCY)


def main_limiter_for(tenant_name: str) -> TokenBucket:
    """Get or create the main-API rate limiter for `tenant_name`."""
    bucket = _main_limiters.get(tenant_name)
    if bucket is None:
        bucket = TokenBucket(rate=MAIN_RPS, capacity=MAIN_RPS)
        _main_limiters[tenant_name] = bucket
    return bucket


def reporting_limiter_for(tenant_name: str) -> TokenBucket:
    """Get or create the reporting-API rate limiter for `tenant_name`."""
    bucket = _reporting_limiters.get(tenant_name)
    if bucket is None:
        bucket = TokenBucket(
            rate=REPORTING_RPM / 60.0,
            capacity=max(1.0, REPORTING_RPM),
        )
        _reporting_limiters[tenant_name] = bucket
    return bucket


class ServiceTitanClient:
    """Wraps authenticated, rate-limited, retrying requests to ServiceTitan."""

    def __init__(
        self,
        app_key: str,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryConfig | None = None,
        main_limiter: TokenBucket,
        reporting_limiter: TokenBucket,
        concurrency: int | None = None,
    ):
        self.app_key = app_key
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.retry = retry or RetryConfig()
        self._main_limiter = main_limiter
        self._reporting_limiter = reporting_limiter
        self._sem = (
            asyncio.Semaphore(concurrency) if concurrency is not None else _concurrency_sem
        )
        if transport is not None:
            # Test path — isolated client with MockTransport.
            self._client = httpx.AsyncClient(transport=transport, timeout=60)
            self._owns_client = True
        else:
            self._client = _get_shared_client()
            self._owns_client = False

    async def _headers(self) -> dict[str, str]:
        token = await token_manager.get_token(
            self.client_id, self.client_secret, self.tenant_id
        )
        return {
            "Authorization": f"Bearer {token}",
            "ST-App-Key": self.app_key,
            "Content-Type": "application/json",
        }

    def _limiter_for(self, path: str) -> TokenBucket:
        return self._reporting_limiter if path.startswith("/reporting/") else self._main_limiter

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | list | None = None,
    ) -> dict:
        url = f"{API_BASE}{path}"
        limiter = self._limiter_for(path)

        attempt = 0
        while True:
            await limiter.acquire()
            headers = await self._headers()
            async with self._sem:
                resp = await self._client.request(
                    method, url, headers=headers, params=params, json=json_body
                )

            if resp.status_code not in self.retry.retry_status:
                _raise_with_body(resp)
                return resp.json()

            # Retry-eligible failure
            if attempt >= self.retry.max_retries:
                _raise_with_body(resp)
                return resp.json()  # unreachable — _raise_with_body raised

            retry_after = _parse_retry_after(resp)
            if retry_after is not None:
                wait = retry_after
            else:
                wait = min(
                    self.retry.base_backoff * (2 ** attempt),
                    self.retry.max_backoff,
                )
            _log(
                f"{resp.status_code} on {method} {path}; "
                f"retry {attempt + 1}/{self.retry.max_retries} after {wait:.2f}s"
            )
            await asyncio.sleep(wait)
            attempt += 1

    async def get(self, path: str, params: dict | None = None, timeout: float = 60) -> dict:
        """GET request. `path` should start with / e.g. /crm/v2/tenant/{tenant}/customers"""
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json_body: dict | list | None = None, timeout: float = 60) -> dict:
        return await self._request("POST", path, json_body=json_body)

    async def patch(self, path: str, json_body: dict | list | None = None, timeout: float = 60) -> dict:
        return await self._request("PATCH", path, json_body=json_body)

    async def put(self, path: str, json_body: dict | list | None = None, timeout: float = 60) -> dict:
        return await self._request("PUT", path, json_body=json_body)

    # -- Convenience helpers --

    async def list_resource(
        self,
        category: str,
        resource: str,
        page: int = 1,
        page_size: int = 50,
        extra_params: dict | None = None,
    ) -> dict:
        """List a paginated resource.

        category: API category slug (crm, jpm, accounting, dispatch, etc.)
        resource: resource path (customers, jobs, invoices, etc.)
        """
        params = {"page": page, "pageSize": page_size}
        if extra_params:
            params.update(extra_params)
        path = f"/{category}/v2/tenant/{self.tenant_id}/{resource}"
        return await self.get(path, params=params)

    async def get_resource(self, category: str, resource: str, resource_id: int) -> dict:
        """Get a single resource by ID."""
        path = f"/{category}/v2/tenant/{self.tenant_id}/{resource}/{resource_id}"
        return await self.get(path)

    async def export_resource(
        self,
        category: str,
        feed: str,
        from_token: str | None = None,
        include_recent_changes: bool = False,
    ) -> dict:
        """Pull one page of a bulk export feed (continuation-token paged).

        Export feeds stream a whole table chronologically by modification date,
        paged via an opaque continuation token rather than page/pageSize. They
        hit the main API bucket (not reporting) — cheap on quota.

        category: API category slug (crm, jpm, accounting, settings, timesheets, ...)
        feed: export feed path (customers, jobs, invoices, customers/contacts, ...)
        from_token: ST `from` param — a `continueFrom` token from a prior
            response, OR a date string (e.g. "2020-01-01") to start at a point in
            time. None/empty starts from the beginning.
        include_recent_changes: ST `includeRecentChanges` — delivers most-recent
            changes faster but may return duplicate items across consecutive calls.
        """
        params: dict = {}
        if from_token:
            params["from"] = from_token
        if include_recent_changes:
            params["includeRecentChanges"] = "true"
        path = f"/{category}/v2/tenant/{self.tenant_id}/export/{feed}"
        return await self.get(path, params=params)
