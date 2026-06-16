"""Tests for the Phase 2 bulk `export_feed` tool.

Two things are locked in here:
  1. The exact ServiceTitan export paths (`/{category}/v2/tenant/123/export/{feed}`)
     and the `from` / `includeRecentChanges` query mapping — guards against a
     Phase-1-style wrong-slug 404 and against sending an empty `from`.
  2. The `_fmt_export` formatter contract the LLM depends on: it surfaces
     `continueFrom` verbatim and the hasMore wait-vs-immediate guidance.

Pattern mirrors `tests/test_get_tools.py`: a MockTransport-backed client patched
onto `server._get_client`, asserting the outgoing request.
"""

from __future__ import annotations

import json

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


def _export_body(items, has_more=False, continue_from="tok-1"):
    return {"hasMore": has_more, "continueFrom": continue_from, "data": items}


# (category, feed) — path is /{category}/v2/tenant/123/export/{feed}
EXPORT_FEEDS = [
    ("crm", "customers"),
    ("crm", "customers/contacts"),  # nested feed keeps its slash
    ("crm", "locations/contacts"),  # nested feed keeps its slash
    ("jpm", "jobs"),
    ("accounting", "invoices"),
    ("accounting", "payments"),
    ("settings", "business-units"),
    ("timesheets", "activities"),
]


@pytest.mark.parametrize(
    "category, feed",
    EXPORT_FEEDS,
    ids=[f"{c}-{f}".replace("/", "_") for c, f in EXPORT_FEEDS],
)
async def test_export_feed_hits_verified_path(monkeypatch, category, feed):
    """export_feed issues GET /{category}/v2/tenant/123/export/{feed} (nested
    feeds keep their slash) and round-trips the {hasMore, continueFrom, data}
    body."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        captured["path"] = request.url.path
        return httpx.Response(200, json=_export_body([{"id": 1}], has_more=True))

    _wire_client(monkeypatch, handler)
    out = await server.export_feed("t", category, feed)

    assert "Error" not in out, out
    assert captured["path"] == f"/{category}/v2/tenant/123/export/{feed}"
    assert '"id": 1' in out
    assert "continueFrom='tok-1'" in out


async def test_no_from_token_omits_from_param(monkeypatch):
    """No from_token → no `from` query param at all (not an empty value)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["from"] = request.url.params.get("from")
        captured["irc"] = request.url.params.get("includeRecentChanges")
        return httpx.Response(200, json=_export_body([]))

    _wire_client(monkeypatch, handler)
    await server.export_feed("t", "crm", "customers")

    assert captured["from"] is None
    assert captured["irc"] is None


async def test_empty_from_token_omits_from_param(monkeypatch):
    """from_token="" is falsy → still omitted (truthy guard in export_resource)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["from"] = request.url.params.get("from")
        return httpx.Response(200, json=_export_body([]))

    _wire_client(monkeypatch, handler)
    await server.export_feed("t", "crm", "customers", from_token="")

    assert captured["from"] is None


async def test_from_token_passed_verbatim(monkeypatch):
    """Both a date string and a continuation token are sent as `from` verbatim."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["from"] = request.url.params.get("from")
        return httpx.Response(200, json=_export_body([]))

    _wire_client(monkeypatch, handler)

    await server.export_feed("t", "crm", "customers", from_token="2020-01-01")
    assert captured["from"] == "2020-01-01"

    await server.export_feed("t", "crm", "customers", from_token="abc.def.ghi")
    assert captured["from"] == "abc.def.ghi"


async def test_include_recent_changes_mapping(monkeypatch):
    """False → no param; True → includeRecentChanges=true (lowercase string)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["irc"] = request.url.params.get("includeRecentChanges")
        return httpx.Response(200, json=_export_body([]))

    _wire_client(monkeypatch, handler)

    await server.export_feed("t", "crm", "customers", include_recent_changes=False)
    assert captured["irc"] is None

    await server.export_feed("t", "crm", "customers", include_recent_changes=True)
    assert captured["irc"] == "true"


# -- _fmt_export formatter contract --


def test_fmt_export_has_more_says_immediately():
    out = server._fmt_export(_export_body([{"id": 1}], has_more=True, continue_from="T9"))
    assert "hasMore=True" in out
    assert "T9" in out
    assert "immediately" in out.lower()


def test_fmt_export_caught_up_says_wait_and_not_loop():
    out = server._fmt_export(
        _export_body([{"id": 1}], has_more=False, continue_from="T9")
    )
    assert "hasMore=False" in out
    assert "T9" in out
    assert "wait 5" in out.lower()
    assert "do not loop" in out.lower()


def test_fmt_export_returns_all_items_no_truncation():
    items = [{"id": i} for i in range(71)]
    out = server._fmt_export(_export_body(items, has_more=True))
    body = out.split("\n\n(Export feed")[0]
    parsed = json.loads(body)
    assert len(parsed) == 71


def test_fmt_export_empty_feed_does_not_crash():
    out = server._fmt_export({"hasMore": False, "data": []})  # no continueFrom key
    body = out.split("\n\n(Export feed")[0]
    assert json.loads(body) == []
    assert "continueFrom=None" in out
