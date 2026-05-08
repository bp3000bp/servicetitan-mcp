"""Tests for the bundled get_lookup_tables tool.

The tool must fan out via asyncio.gather across every kind in
`_LOOKUP_KINDS`, return one section per kind in the JSON output, surface
per-kind errors without aborting the whole batch, and reject unknown
kinds before issuing any API calls.
"""

from __future__ import annotations

import json

from servicetitan_mcp import server


class FakeClient:
    """Records every list_resource call and returns canned data per (cat, res)."""

    def __init__(self, fixtures: dict[tuple[str, str], dict] | None = None):
        self.calls: list[tuple[str, str, int, int]] = []
        self._fixtures = fixtures or {}

    async def list_resource(self, category, resource, page=1, page_size=50, params=None):
        self.calls.append((category, resource, page, page_size))
        return self._fixtures.get(
            (category, resource),
            {
                "data": [],
                "totalCount": 0,
                "page": page,
                "pageSize": page_size,
                "hasMore": False,
            },
        )


async def test_default_kinds_fan_out_to_every_lookup_table(monkeypatch):
    """With no `kinds` arg, the tool fires one list_resource per kind in
    `_LOOKUP_KINDS` and the output JSON has a section for each."""

    fake = FakeClient()
    monkeypatch.setattr(server, "_get_client", lambda _t: fake)

    output = await server.get_lookup_tables(tenant="any")

    expected_pairs = set(server._LOOKUP_KINDS.values())
    actual_pairs = {(c, r) for (c, r, _p, _ps) in fake.calls}
    assert actual_pairs == expected_pairs, (
        f"missing or extra fan-out targets. expected={expected_pairs} actual={actual_pairs}"
    )
    assert len(fake.calls) == len(server._LOOKUP_KINDS)

    parsed = json.loads(output)
    for kind in server._LOOKUP_KINDS:
        assert kind in parsed, f"missing {kind} in output keys: {list(parsed)}"
    assert "_meta" in parsed
    assert parsed["_meta"]["kinds_fetched"] == list(server._LOOKUP_KINDS)


async def test_subset_kinds_only_fetches_requested(monkeypatch):
    """Passing a subset triggers exactly those calls — no extras."""

    fixtures = {
        ("settings", "business-units"): {
            "data": [{"id": 1, "name": "Plumbing"}],
            "totalCount": 1,
        },
        ("jpm", "job-types"): {
            "data": [{"id": 9, "name": "Service"}],
            "totalCount": 1,
        },
    }
    fake = FakeClient(fixtures)
    monkeypatch.setattr(server, "_get_client", lambda _t: fake)

    output = await server.get_lookup_tables(
        tenant="any", kinds=["business_units", "job_types"]
    )

    assert {(c, r) for (c, r, _p, _ps) in fake.calls} == {
        ("settings", "business-units"),
        ("jpm", "job-types"),
    }

    parsed = json.loads(output)
    assert parsed["business_units"]["count"] == 1
    assert parsed["business_units"]["items"][0]["name"] == "Plumbing"
    assert parsed["job_types"]["count"] == 1
    assert "zones" not in parsed
    assert "warehouses" not in parsed


async def test_page_size_is_threaded_through(monkeypatch):
    """The caller's `page_size` reaches `list_resource` — without it, large
    static tables (e.g. campaigns) would silently truncate."""

    fake = FakeClient()
    monkeypatch.setattr(server, "_get_client", lambda _t: fake)

    await server.get_lookup_tables(
        tenant="any", kinds=["business_units"], page_size=500
    )

    assert fake.calls == [("settings", "business-units", 1, 500)]


async def test_unknown_kind_short_circuits_without_calling_api(monkeypatch):
    """Unknown kinds must be rejected before any fan-out — otherwise a typo
    silently fetches whatever happens to be valid and hides the rest."""

    fake = FakeClient()
    monkeypatch.setattr(server, "_get_client", lambda _t: fake)

    output = await server.get_lookup_tables(
        tenant="any", kinds=["zones", "not_a_real_kind"]
    )

    parsed = json.loads(output)
    assert "error" in parsed
    assert "not_a_real_kind" in parsed["error"]
    assert "valid_kinds" in parsed
    assert fake.calls == [], "no API calls should fan out when input is invalid"


async def test_one_kind_failure_does_not_block_other_kinds(monkeypatch):
    """gather() with per-kind try/except must let the surviving kinds
    return data even if one raises."""

    class FlakyClient(FakeClient):
        async def list_resource(self, category, resource, page=1, page_size=50, params=None):
            if (category, resource) == ("settings", "business-units"):
                raise RuntimeError("boom")
            return await FakeClient.list_resource(
                self, category, resource, page, page_size, params
            )

    fixtures = {
        ("dispatch", "zones"): {
            "data": [{"id": 7, "name": "North"}],
            "totalCount": 1,
        },
    }
    fake = FlakyClient(fixtures)
    monkeypatch.setattr(server, "_get_client", lambda _t: fake)

    output = await server.get_lookup_tables(
        tenant="any", kinds=["business_units", "zones"]
    )

    parsed = json.loads(output)
    assert "error" in parsed["business_units"]
    assert "boom" in parsed["business_units"]["error"]
    assert parsed["zones"]["count"] == 1
    assert parsed["zones"]["items"][0]["name"] == "North"


def test_lookup_kinds_covers_documented_static_tables():
    """The bundled tool must cover every static-config table the bullet #6
    instructions point the LLM at — otherwise the LLM follows the instruction
    and finds the bundle is missing what it needed."""

    documented = {
        "business_units",
        "job_types",
        "zones",
        "warehouses",
        "payment_types",
        "tax_zones",
        "payment_terms",
        "membership_types",
        "tag_types",
        "activity_categories",
        "pricebook_categories",
        "inventory_vendors",
        "trucks",
        "user_roles",
        "campaigns",
    }
    assert set(server._LOOKUP_KINDS) >= documented, (
        f"_LOOKUP_KINDS missing documented kinds: {documented - set(server._LOOKUP_KINDS)}"
    )
