"""Tests for the report-discovery tools: `list_reports_in_category`,
`get_report`, and `get_report_parameter_values`.

These lock in the fix for a previously-uncallable tool
(`list_reports_in_category` typed its category as an int and built a
pluralized `report-categories` path that 404s) and the two new tools that
make running an arbitrary report self-service: single-report metadata and
dynamic-value-set resolution.
"""

from __future__ import annotations

import httpx
import pytest

from servicetitan_mcp import auth, server
from servicetitan_mcp.client import RetryConfig, ServiceTitanClient, TokenBucket


@pytest.fixture(autouse=True)
def _stub_token(monkeypatch):
    """Avoid real network auth. Every test gets a dummy bearer token."""

    async def fake_get_token(*_a, **_kw):
        return "fake-token"

    monkeypatch.setattr(auth.token_manager, "get_token", fake_get_token)
    yield


def _wire_client(monkeypatch, handler) -> ServiceTitanClient:
    """Build a MockTransport-backed client and patch `_get_client` to return it."""
    transport = httpx.MockTransport(handler)
    c = ServiceTitanClient(
        app_key="k",
        client_id="ci",
        client_secret="cs",
        tenant_id="123",
        transport=transport,
        retry=RetryConfig(max_retries=0, base_backoff=0.01),
        main_limiter=TokenBucket(rate=1000, capacity=1000),
        reporting_limiter=TokenBucket(rate=1000, capacity=1000),
    )
    monkeypatch.setattr(server, "_get_client", lambda _tenant: c)
    return c


# ── list_reports_in_category ──────────────────────────────────────────


async def test_list_reports_in_category_uses_singular_path_and_string_slug(monkeypatch):
    """Regression guard for BOTH original bugs: the path must be the
    singular `report-category` (not `report-categories`) and the category
    must be accepted as a string slug."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        captured["path"] = request.url.path
        return httpx.Response(200, json={"data": [], "page": 1, "pageSize": 50, "totalCount": 0, "hasMore": False})

    _wire_client(monkeypatch, handler)
    out = await server.list_reports_in_category(tenant="t", category="operations")

    assert "Error" not in out, out
    assert captured["path"] == "/reporting/v2/tenant/123/report-category/operations/reports"
    # The pluralized form was the bug — make sure it's gone.
    assert "report-categories" not in captured["path"]


# ── get_report ────────────────────────────────────────────────────────


async def test_get_report_builds_single_report_path(monkeypatch):
    """`get_report` targets one report's metadata endpoint (no /data)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": 3749, "parameters": [], "fields": []})

    _wire_client(monkeypatch, handler)
    out = await server.get_report(tenant="t", category="operations", report_id=3749)

    assert "Error" not in out, out
    assert captured["path"] == "/reporting/v2/tenant/123/report-category/operations/reports/3749"
    assert not captured["path"].endswith("/data")


# ── get_report_parameter_values ───────────────────────────────────────


async def test_get_report_parameter_values_builds_dynamic_set_path(monkeypatch):
    """Resolver hits the dynamic-value-sets endpoint with the set id."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        captured["path"] = request.url.path
        return httpx.Response(200, json=[[0, "Invoice Date"], [1, "Job Completion Date"]])

    _wire_client(monkeypatch, handler)
    out = await server.get_report_parameter_values(tenant="t", dynamic_set_id="job-date-filter-type")

    assert "Error" not in out, out
    assert captured["path"] == "/reporting/v2/tenant/123/dynamic-value-sets/job-date-filter-type"
    assert "Invoice Date" in out


# ── Schema lock ───────────────────────────────────────────────────────


async def test_list_reports_in_category_schema_uses_string_category():
    """Lock the contract: `list_reports_in_category` must advertise a
    string `category` param and must NOT carry the old `category_id`."""
    tools = await server.mcp.list_tools()
    by_name = {t.name: t for t in tools}

    schema = by_name["list_reports_in_category"].inputSchema
    props = schema["properties"]

    assert "category_id" not in props, "the old int `category_id` arg must be gone"
    assert "category" in props, "must expose a `category` arg"
    assert props["category"].get("type") == "string", (
        f"category must be string-typed: {props['category']}"
    )
