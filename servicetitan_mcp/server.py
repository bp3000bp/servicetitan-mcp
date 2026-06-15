"""ServiceTitan MCP Server — multi-tenant integration for Claude.

Environment variables (set in claude_desktop_config.json or .env):
  ST_TENANTS                      — comma-separated tenant names (e.g.
                                    `acme,other`)
  ST_TENANT_<NAME>_ID             — Tenant ID for <NAME>
  ST_TENANT_<NAME>_CLIENT_ID      — Client ID for <NAME>
  ST_TENANT_<NAME>_CLIENT_SECRET  — Client Secret for <NAME>
  ST_TENANT_<NAME>_APP_KEY        — App Key from the developer portal

Every @mcp.tool() takes a required `tenant` argument naming one of the
configured tenants. Call `list_tenants` to discover the names.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback

from mcp.server.fastmcp import FastMCP

from .client import ServiceTitanClient, main_limiter_for, reporting_limiter_for
from .config import get_tenant, tenant_names

# ── Bootstrap ────────────────────────────────────────────────────────

mcp = FastMCP(
    "ServiceTitan",
    instructions="""ServiceTitan field-service API: customers, jobs, invoices, estimates, dispatch, pricebook, payroll, memberships, reporting.

HOW TO USE THIS SERVER EFFICIENTLY:

1. PREFER FILTERS OVER LISTING-THEN-SCANNING. Most `list_*` tools accept
   server-side filters (name, status, customer_id, date ranges). Use them.
   Listing every customer then filtering locally wastes API quota and tokens.

2. PAGINATION. `list_*` tools default to page_size=200 (run_report stays at
   50 — its quota is much tighter). `page_size` is passed directly to
   ServiceTitan; most endpoints accept hundreds to a few thousand per page
   (ST enforces its own per-endpoint cap). For full-table scans, raise
   page_size further (500–1000) rather than walking many pages. ALWAYS trust
   the `hasMore=…` footer — NOT the number of items shown — to decide
   whether more pages exist. `totalCount` may be reported as `unknown` for
   endpoints that don't return it; in that case `hasMore` is inferred from
   whether a full page came back, so keep paging until `hasMore=False`.

3. RATE LIMITS ARE HANDLED FOR YOU. The client retries 429s with backoff, so
   transient throttling is invisible. But you can still exhaust quotas:
     - Main API: ~30 req/sec (soft cap, ST allows 60/sec)
     - Reporting API: ~3 req/min (ST hard cap is 5/min) — run_report is SLOW
   If you see a surfaced 429, reduce parallel tool calls or batch via filters.

4. REPORTING IS A LAST RESORT. Prefer domain-specific tools (list_invoices,
   list_jobs, list_payments) with date filters over `run_report`. Reports are
   slow, quota-constrained, and return schemas that vary by report_id. Only
   reach for `run_report` when no typed tool can answer the question (e.g.
   aggregated revenue breakdowns). To run a specific report end-to-end:
   `get_report(category, report_id)` for its parameter schema → for any param
   with an `acceptValues.dynamicSetId`, `get_report_parameter_values(setId)`
   to get the legal value → `run_report(...)`. (Discover ids first via
   `list_report_categories` → `list_reports_in_category(category)` if unknown.)
   Each is a reporting-bucket call, so resolve value sets once and reuse them.

5. FIND-BEFORE-GET. `get_customer(customer_id)` needs an ID you don't know
   upfront. Use `list_customers(name=...)` to find the ID, then `get_customer`
   only if you need the full record.

6. STATIC LOOKUPS ARE CACHEABLE WITHIN A SESSION. When you need IDs from
   several of these tables, call `get_lookup_tables` once — it bundles the
   fan-out into one tool call. Otherwise, individual tools whose docstring
   contains the phrase "static config" (`list_business_units`,
   `list_job_types`, `list_zones`, `list_warehouses`, `list_payment_types`,
   `list_tax_zones`, `list_payment_terms`, `list_membership_types`,
   `list_tag_types`, `list_activity_categories`, `list_pricebook_categories`,
   `list_inventory_vendors`, `list_trucks`, `list_user_roles`,
   `list_campaigns`) rarely change. Call once per conversation and reuse
   the IDs — re-fetching them on every question wastes calls. If your
   client supports MCP resources, prefer reading
   `servicetitan://lookups/{tenant}/{kind}` (or `servicetitan://tenants`
   for tenant discovery) over the corresponding `list_*` tool — same
   data, no tool-call charge, and the server caches each (tenant, kind)
   for an hour.

7. `list_*` USUALLY BEATS `get_*`. The list payload almost always contains
   every field `get_*` would return for the same record. Only call `get_*`
   when the docstring explicitly says you need extended fields not in the
   list response (line items on `get_invoice` / `get_estimate`, full custom
   fields and tags on `get_job`, full contact blocks on `get_customer`).

8. ESCAPE HATCH. `servicetitan_api_call` exposes arbitrary endpoints (use
   `{tenant_id}` placeholder). Use when a typed tool doesn't cover the
   endpoint you need — e.g. unusual search params, POST/PATCH writes, or
   beta endpoints. Check ServiceTitan's API docs for the exact path.
""",
)

_CLIENT_CACHE: dict[str, ServiceTitanClient] = {}


def _get_client(tenant: str) -> ServiceTitanClient:
    """Get or build the cached client for `tenant`.

    Credentials come from config.get_tenant, which raises UnknownTenantError
    (a ValueError) when the tenant isn't configured. Caching one client per
    tenant preserves connection pooling, per-tenant rate-limit buckets, and
    per-tenant token caching.
    """
    name = tenant.strip().lower()
    if name not in _CLIENT_CACHE:
        creds = get_tenant(name)
        _CLIENT_CACHE[name] = ServiceTitanClient(
            creds.app_key,
            creds.client_id,
            creds.client_secret,
            creds.tenant_id,
            main_limiter=main_limiter_for(name),
            reporting_limiter=reporting_limiter_for(name),
        )
    return _CLIENT_CACHE[name]


def _resolve(tenant: str) -> ServiceTitanClient:
    """Tool-facing client resolver.

    Use from @mcp.tool() handlers. Raises UnknownTenantError (a ValueError
    subclass) if the tenant isn't configured; the message lists valid names.
    """
    return _get_client(tenant)


@mcp.tool()
def list_tenants() -> str:
    """List configured ServiceTitan tenants (names only; no IDs or secrets).

    When to use: call this first when the user names a specific business
    or asks for a cross-tenant aggregation (e.g. "total across all
    businesses"). Every other tool requires a `tenant` argument — use a
    name from this list.
    """
    return json.dumps({"tenants": tenant_names()}, indent=2)


def _fmt(data: dict | list) -> str:
    """Pretty-print API response and expose pagination state.

    Returns every item ServiceTitan returned — the caller's `page_size` is the
    only knob that governs output size. We do NOT further truncate here, because
    a hidden cap causes silent undercounts: callers assume `hasMore=False` means
    the result is complete when it actually only means "no more API pages."

    The trailer reports page, pageSize, totalCount, and hasMore so the caller
    can decide whether to fetch another page.
    """
    if isinstance(data, dict) and "data" in data:
        items = data["data"]
        raw_total = data.get("totalCount")
        total_display = raw_total if raw_total is not None else "unknown"
        page = data.get("page", 1)
        page_size = data.get("pageSize", len(items))
        has_more = data.get("hasMore")
        if has_more is None:
            # ST didn't send hasMore — infer.
            if raw_total is not None:
                has_more = (page * page_size) < raw_total
            else:
                # Without a total, a full page is the only signal of "more".
                has_more = len(items) >= page_size > 0
        note = (
            f"\n\n(Showing {len(items)} of {total_display} results — "
            f"page {page}, pageSize {page_size}, hasMore={has_more})"
        )
        return json.dumps(items, indent=2, default=str) + note
    return json.dumps(data, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════
#  CRM — Customers, Contacts, Locations, Leads, Bookings
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_customers(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    name: str | None = None,
    active_only: bool = True,
) -> str:
    """Search/list customer accounts (the billing entity, not individuals).

    When to use: starting from a customer's name, or enumerating customers for
    bulk analysis. The `name` param filters server-side (substring match).
    When NOT: if you already have a numeric customer_id, call `get_customer`
    instead. For individuals tied to a customer, use `list_contacts`.
    Note: ST has no phone/email filter on this endpoint — for those, use
    `servicetitan_api_call` with a specific search endpoint or search via a
    broader `list_contacts` call and match locally.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if name:
        params["name"] = name
    if active_only:
        params["active"] = "True"
    data = await client.list_resource("crm", "customers", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def get_customer(tenant: str, customer_id: int) -> str:
    """Get the full record for one customer by numeric ID.

    When to use: you already have an ID (e.g. from `list_customers`,
    `list_jobs`, or an invoice) and need contact blocks, addresses, balance,
    or custom fields.
    When NOT: if you only know the name, call `list_customers(name=...)`
    first — that returns enough detail for most lookups without the follow-up.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.get_resource("crm", "customers", customer_id)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
async def list_locations(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    customer_id: int | None = None,
) -> str:
    """List service locations (physical addresses a customer owns).

    When to use: a customer has multiple sites and you need to pick one, or
    enumerating locations for route/territory analysis.
    When NOT: a customer with a single location — the address is already
    inside the `list_customers` / `get_customer` response.
    Tip: always pass `customer_id` when you have it — otherwise this returns
    every location in the tenant.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if customer_id:
        params["customerId"] = customer_id
    data = await client.list_resource("crm", "locations", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def get_location(tenant: str, location_id: int) -> str:
    """Get the full record for one service location by ID.

    When to use: you have a location_id (from a job or `list_locations`) and
    need site-specific details (zone, access notes, installed equipment link).
    When NOT: if looking up by address, use `list_locations` with
    `customer_id`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.get_resource("crm", "locations", location_id)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
async def list_leads(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    status: str | None = None,
) -> str:
    """List leads/sales opportunities (not yet converted to jobs).

    When to use: pipeline questions ("how many open leads", "which leads
    stalled this month"). Filter by `status` (Open, Won, Lost, Dismissed).
    When NOT: for work that's already been booked as a job, use `list_jobs`.
    For booking call intake, use `list_bookings`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if status:
        params["status"] = status
    data = await client.list_resource("crm", "leads", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_bookings(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    status: str | None = None,
) -> str:
    """List inbound bookings (call-in / web-form requests awaiting scheduling).

    When to use: front-office / CSR questions — what came in today, which
    bookings are still Pending. Status values: Pending, Scheduled, Converted,
    Dismissed.
    When NOT: if the booking has already been converted to a scheduled job,
    query `list_jobs` with a date filter instead.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if status:
        params["status"] = status
    data = await client.list_resource("crm", "bookings", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_contacts(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    customer_id: int | None = None,
) -> str:
    """List individual contacts (phone/email) attached to customer accounts.

    When to use: finding who to call for a given customer_id, or enumerating
    contacts for a marketing export. Always pass `customer_id` when known —
    without it, this returns every contact in the tenant (many pages).
    When NOT: if the customer only has one primary contact, `get_customer`
    already includes it inline.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if customer_id:
        params["customerId"] = customer_id
    data = await client.list_resource("crm", "contacts", page, page_size, params)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  JOB PLANNING & MANAGEMENT — Jobs, Appointments, Projects
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_jobs(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    status: str | None = None,
    customer_id: int | None = None,
    created_on_or_after: str | None = None,
    completed_on_or_after: str | None = None,
) -> str:
    """List jobs (scheduled or completed work orders). THE core work entity.

    When to use: "what jobs for customer X", "jobs completed last week",
    "open in-progress jobs". Always pass at least one filter (status,
    customer_id, or a date) — an unfiltered list is huge.
    When NOT: if you want the individual scheduling slots, use
    `list_appointments` (one job can have multiple appointments). For
    technician → appointment assignments, use `list_appointment_assignments`.

    status values: Scheduled, InProgress, Hold, Completed, Canceled.
    Date format: YYYY-MM-DD.

    page_size is passed through to ServiceTitan (no client-side cap). For
    attribution / counting work, pick a page_size large enough to return every
    record in one call, then verify with the `hasMore=…` footer — do NOT infer
    totals from the number of items displayed.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if status:
        params["jobStatus"] = status
    if customer_id:
        params["customerId"] = customer_id
    if created_on_or_after:
        params["createdOnOrAfter"] = created_on_or_after
    if completed_on_or_after:
        params["completedOnOrAfter"] = completed_on_or_after
    data = await client.list_resource("jpm", "jobs", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def get_job(tenant: str, job_id: int) -> str:
    """Get full details for one job by ID.

    When to use: you have a job_id and need customer/location/appointment/
    invoice links plus custom fields.
    When NOT: `list_jobs` already returns enough for summary work — only
    call `get_job` when you need a specific field (tags, custom fields,
    full notes) not present in the list response.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.get_resource("jpm", "jobs", job_id)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
async def list_appointments(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    starts_on_or_after: str | None = None,
    starts_on_or_before: str | None = None,
) -> str:
    """List appointment slots (a scheduled visit; one job can have many).

    When to use: "what's on the calendar tomorrow", "reschedule count this
    week". ALWAYS pass a date range — a full-tenant appointment list is
    very large.
    When NOT: to see who's assigned to run an appointment, use
    `list_appointment_assignments`. For non-customer-facing time blocks
    (meetings, training), use `list_non_job_appointments`.

    Date format: YYYY-MM-DD.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if starts_on_or_after:
        params["startsOnOrAfter"] = starts_on_or_after
    if starts_on_or_before:
        params["startsOnOrBefore"] = starts_on_or_before
    data = await client.list_resource("jpm", "appointments", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_job_types(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List all job-type definitions in ServiceTitan — static config.

    When to use: mapping a jobTypeId from a job record to its human name;
    enumerating service offerings.
    When NOT: don't call this to find actual jobs — use `list_jobs` instead.
    The list is small and static; cache the result in your working memory.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("jpm", "job-types", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_projects(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    status: str | None = None,
) -> str:
    """List multi-job projects (umbrella entity that groups related jobs).

    When to use: commercial / construction contexts where one engagement
    spans multiple jobs. Usually empty for pure residential service shops.
    When NOT: residential-only tenants rarely use projects — check by
    running once without a filter to see if any exist.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if status:
        params["status"] = status
    data = await client.list_resource("jpm", "projects", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def get_project(tenant: str, project_id: int) -> str:
    """Get one project (multi-job umbrella) by ID.

    When to use: you have a project_id and need rollup details across its
    child jobs. See `list_projects` to discover IDs first.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.get_resource("jpm", "projects", project_id)
    return json.dumps(data, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════
#  ACCOUNTING — Invoices, Payments, Bills
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_invoices(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    job_id: int | None = None,
    customer_id: int | None = None,
    created_on_or_after: str | None = None,
) -> str:
    """List invoices (AR — money owed by customers).

    When to use: "invoice for job X", "customer's invoices this quarter",
    "revenue since date Y". Always filter — unfiltered is huge. For
    aggregate revenue across BUs, prefer a `run_report` call if the
    breakdown matters (slower but pre-aggregated).
    When NOT: for payments RECEIVED (the cash side), use `list_payments`.
    For vendor bills (AP), use `list_inventory_bills`.

    Tip: the list response includes totals; `get_invoice` is only needed
    for line-item detail.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if job_id:
        params["jobId"] = job_id
    if customer_id:
        params["customerId"] = customer_id
    if created_on_or_after:
        params["createdOnOrAfter"] = created_on_or_after
    data = await client.list_resource("accounting", "invoices", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def get_invoice(tenant: str, invoice_id: int) -> str:
    """Get one invoice by ID with full line items.

    When to use: you need each line (service, material, equipment) with
    quantities and prices — e.g. to answer "what was actually sold on this
    job".
    When NOT: for totals and customer/job linkage, `list_invoices` already
    has enough.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.get_resource("accounting", "invoices", invoice_id)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
async def list_payments(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    created_on_or_after: str | None = None,
) -> str:
    """List payments received from customers (cash in).

    When to use: cash-flow questions, deposit reconciliation, "what came in
    yesterday". Use with `created_on_or_after` for date slices.
    When NOT: for amounts invoiced (billed but not necessarily collected),
    use `list_invoices`. Those are distinct accounting events.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if created_on_or_after:
        params["createdOnOrAfter"] = created_on_or_after
    data = await client.list_resource("accounting", "payments", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_payment_types(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List configured payment types (Cash, Check, Visa, ACH, etc.) — static config.

    When to use: resolving a paymentTypeId from a `list_payments` row.
    When NOT: small, static; cache the result rather than calling repeatedly.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("accounting", "payment-types", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_inventory_bills(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    created_on_or_after: str | None = None,
) -> str:
    """List AP bills from vendors (inventory purchases, cost side).

    When to use: vendor spend analysis, AP aging.
    When NOT: for customer-facing revenue, use `list_invoices` /
    `list_payments`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if created_on_or_after:
        params["createdOnOrAfter"] = created_on_or_after
    data = await client.list_resource("accounting", "inventory-bills", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_journal_entries(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    created_on_or_after: str | None = None,
) -> str:
    """List GL journal entries posted from ServiceTitan.

    When to use: GL reconciliation against an external accounting system
    (QuickBooks, NetSuite). Bookkeeper-level detail.
    When NOT: for operational P&L questions, use reports or
    `list_invoices` / `list_payments`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if created_on_or_after:
        params["createdOnOrAfter"] = created_on_or_after
    data = await client.list_resource("accounting", "journal-entries", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_payment_terms(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List configured payment terms (Net 30, Due on Receipt, etc.) — static config.

    When to use: resolving paymentTermsId on an invoice.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("accounting", "payment-terms", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_tax_zones(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List configured sales tax zones — static config.

    When to use: resolving a taxZoneId or auditing tax configuration.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("accounting", "tax-zones", page, page_size)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  SALES & ESTIMATES
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_estimates(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    job_id: int | None = None,
    status: str | None = None,
    sold_after: str | None = None,
) -> str:
    """List estimates / proposals (pre-sale quotes).

    When to use: "open estimates", "estimates sold this month", or
    estimates for a given job. Status values: Open, Sold, Dismissed.
    `sold_after` (YYYY-MM-DD) is the right filter for close-rate analysis.
    When NOT: once an estimate is `Sold`, the work lives in `list_jobs`
    and the money in `list_invoices`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if job_id:
        params["jobId"] = job_id
    if status:
        params["status"] = status
    if sold_after:
        params["soldAfter"] = sold_after
    data = await client.list_resource("sales", "estimates", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def get_estimate(tenant: str, estimate_id: int) -> str:
    """Get one estimate by ID with line items.

    When to use: you need the proposed line items / options on a quote
    (often multiple Good/Better/Best tiers).
    When NOT: for totals and status, `list_estimates` is enough.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.get_resource("sales", "estimates", estimate_id)
    return json.dumps(data, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════
#  DISPATCH — Appointments, Technician Shifts, Zones
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_appointment_assignments(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    starts_on_or_after: str | None = None,
    starts_on_or_before: str | None = None,
) -> str:
    """List technician-to-appointment assignments (who is running which slot).

    When to use: dispatcher questions — "who's on this job", "tech's
    schedule today". Always pass a date range.
    When NOT: for the appointment slot itself (time, job, customer), use
    `list_appointments`. For planned shift availability (not actual dispatch),
    use `list_technician_shifts`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if starts_on_or_after:
        params["startsOnOrAfter"] = starts_on_or_after
    if starts_on_or_before:
        params["startsOnOrBefore"] = starts_on_or_before
    data = await client.list_resource("dispatch", "appointment-assignments", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_technician_shifts(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    starts_on_or_after: str | None = None,
    starts_on_or_before: str | None = None,
) -> str:
    """List scheduled shifts (planned availability blocks, not dispatch).

    When to use: capacity planning — "who's working Saturday", shift-fill
    analysis.
    When NOT: for actual on-the-day dispatch, use
    `list_appointment_assignments`. For time-clock actuals, use
    `list_activities` (timesheets).

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if starts_on_or_after:
        params["startsOnOrAfter"] = starts_on_or_after
    if starts_on_or_before:
        params["startsOnOrBefore"] = starts_on_or_before
    data = await client.list_resource("dispatch", "technician-shifts", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_zones(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List dispatch zones (geographic service areas) — static config.

    When to use: resolving zoneId from a location or technician record.
    Cache the result.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("dispatch", "zones", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_non_job_appointments(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    starts_on_or_after: str | None = None,
) -> str:
    """List internal time blocks that aren't customer work (training, meetings).

    When to use: understanding why a tech's calendar is "full" without
    billable work, capacity blocked for non-revenue reasons.
    When NOT: for customer-facing appointments, use `list_appointments`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if starts_on_or_after:
        params["startsOnOrAfter"] = starts_on_or_after
    data = await client.list_resource("dispatch", "non-job-appointments", page, page_size, params)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  PRICEBOOK — Services, Materials, Equipment, Categories
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_pricebook_services(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    active_only: bool = True,
) -> str:
    """List pricebook SERVICES (labor / diagnostic line items techs sell).

    When to use: pricebook audit ("what services are we selling"), margin
    analysis, service catalog exports. Services typically have a fixed
    price + billable time component.
    When NOT: for physical parts, use `list_pricebook_materials`; for
    replaceable units (a water heater, a furnace), use
    `list_pricebook_equipment`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if active_only:
        params["active"] = "True"
    data = await client.list_resource("pricebook", "services", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_pricebook_materials(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    active_only: bool = True,
) -> str:
    """List pricebook MATERIALS (consumable parts: pipe, fittings, filters).

    When to use: parts catalog audit, vendor cost reconciliation.
    When NOT: for larger replaceable units (water heaters, HVAC
    equipment), use `list_pricebook_equipment`. For labor / diagnostic
    charges, use `list_pricebook_services`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if active_only:
        params["active"] = "True"
    data = await client.list_resource("pricebook", "materials", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_pricebook_equipment(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    active_only: bool = True,
) -> str:
    """List pricebook EQUIPMENT (big-ticket installable units: AC, furnace, water heater).

    When to use: replacement-project catalog, installation SKU lookup.
    When NOT: for consumable parts, use `list_pricebook_materials`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if active_only:
        params["active"] = "True"
    data = await client.list_resource("pricebook", "equipment", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_pricebook_categories(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List pricebook categories (folder structure in the pricebook) — static config.

    When to use: resolving categoryId to a human name, or browsing the
    catalog hierarchy.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("pricebook", "categories", page, page_size)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  INVENTORY — Purchase Orders, Warehouses, Vendors, Trucks
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_purchase_orders(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    status: str | None = None,
) -> str:
    """List purchase orders (POs to vendors for parts/equipment).

    When to use: PO aging, open PO audit. Status values: Pending, Sent,
    PartiallyReceived, Received, Canceled.
    When NOT: for the received bills, use `list_inventory_bills`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if status:
        params["status"] = status
    data = await client.list_resource("inventory", "purchase-orders", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_warehouses(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List inventory warehouses (storage locations) — static config.

    When to use: resolving warehouseId on a PO or stock record.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("inventory", "warehouses", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_inventory_vendors(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List vendors / suppliers — static config.

    When to use: resolving vendorId on a PO or bill.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("inventory", "vendors", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_trucks(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List trucks (mobile warehouses tied to technicians) — static config.

    When to use: fleet / rolling-stock audit, resolving truckId on an
    inventory transfer.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("inventory", "trucks", page, page_size)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  MEMBERSHIPS — Recurring Revenue
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_memberships(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    status: str | None = None,
) -> str:
    """List customer memberships (maintenance agreements tied to a customer).

    When to use: churn / renewal questions, revenue tied to active plans.
    Status values: Active, Expired, Canceled, Deleted, Suspended.
    When NOT: for the membership CATALOG (plan definitions), use
    `list_membership_types`. For the recurring visits scheduled under each
    membership, use `list_recurring_services`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if status:
        params["status"] = status
    data = await client.list_resource("memberships", "memberships", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_membership_types(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List membership PLAN definitions (Gold, Silver, etc.) — static config.

    When to use: resolving membershipTypeId, auditing what plans exist.
    When NOT: for actual customer memberships, use `list_memberships`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("memberships", "membership-types", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_recurring_services(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
) -> str:
    """List recurring services (tune-ups scheduled under memberships).

    When to use: "upcoming maintenance visits", tune-up schedule planning.
    When NOT: if you want the membership itself, use `list_memberships`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("memberships", "recurring-services", page, page_size)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  SETTINGS — Employees, Technicians, Business Units, Tags
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_employees(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    active_only: bool = True,
) -> str:
    """List all employees (office staff + techs; superset of technicians).

    When to use: headcount questions, looking up anyone with a ServiceTitan
    login (CSRs, dispatchers, managers).
    When NOT: for field techs specifically (who can be dispatched on jobs),
    use `list_technicians` — it's a filtered subset with tech-only fields
    like skills and licenses.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if active_only:
        params["active"] = "True"
    data = await client.list_resource("settings", "employees", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def get_employee(tenant: str, employee_id: int) -> str:
    """Get one employee by ID.

    When to use: you have an employeeId (from a job, payroll row, or
    activity) and need full contact / role / login details.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.get_resource("settings", "employees", employee_id)
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
async def list_technicians(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    active_only: bool = True,
) -> str:
    """List field technicians (subset of employees who run jobs).

    When to use: dispatch / crew questions, "how many active techs",
    technician performance rollups.
    When NOT: for non-tech staff (CSRs, managers), use `list_employees`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if active_only:
        params["active"] = "True"
    data = await client.list_resource("settings", "technicians", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_business_units(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List business units — top-level revenue buckets: HVAC, Plumbing, etc. — static config.

    When to use: almost always needed when slicing revenue or jobs by BU —
    BU ids show up on nearly every job, invoice, and report. Cache the
    mapping (id → name) once per session.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("settings", "business-units", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_tag_types(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List tag-type definitions (color labels applied to customers/jobs/locations) — static config.

    When to use: resolving a tagTypeId to human label for filtering.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("settings", "tag-types", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_user_roles(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List user-role definitions (permission groups) — static config.

    When to use: auditing who has what permission level.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("settings", "user-roles", page, page_size)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  REPORTING — Dynamic Reports
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_report_categories(tenant: str) -> str:
    """Discover report CATEGORIES (first step in the reporting flow).

    When to use: you don't know which report to run yet. Categories are
    broad groupings (Marketing, Operations, Accounting, etc.). Each has a
    string `id` slug (e.g. "operations").
    Next step: call `list_reports_in_category(category)` with that slug to
    find a specific report.

    Rate-limit note: lives under the reporting quota (~3 req/min in this
    server). Batch reporting work together.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("reporting", "report-categories", 1, 200)
    return _fmt(data)


@mcp.tool()
async def list_reports_in_category(tenant: str, category: str) -> str:
    """List reports inside one category, along with their parameter schemas.

    PREREQUISITE: call list_report_categories first to get a valid
    category slug. Do NOT guess or invent one — passing an unknown
    category causes a confusing 404 from ServiceTitan.

    When to use: you've found a category and need to know (a) which
    report_id to run and (b) what parameters that report requires.
    Read the `parameters` block carefully — parameter names and value
    types vary per report. If a parameter has an `acceptValues.dynamicSetId`,
    resolve it with `get_report_parameter_values` to get the legal values.

    If you already know the report_id, skip this and call `get_report`
    (category, report_id) — it returns one report's schema directly without
    paging the whole category.

    category: the string slug from `list_report_categories` (e.g.
      "operations", "accounting") — NOT a numeric id.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    path = f"/reporting/v2/tenant/{client.tenant_id}/report-category/{category}/reports"
    data = await client.get(path)
    return _fmt(data)


@mcp.tool()
async def get_report(tenant: str, category: str, report_id: int) -> str:
    """Get one report's parameter schema and output fields by id.

    When to use: you already know the report's `category` slug and
    `report_id` and want its input parameters + output columns directly.
    Cheaper and more targeted than `list_reports_in_category`, which pages
    every report in the category.

    Read the returned `parameters`: each has a `name` (what you pass to
    `run_report`), `dataType`, `isArray`, and `isRequired`. When a
    parameter carries `acceptValues.dynamicSetId` (e.g. a "Filter by" /
    DateType code, or a Business Unit picker), pass that id to
    `get_report_parameter_values` to get the allowed values.

    category: the string slug from `list_report_categories` (e.g.
      "operations") — NOT a numeric id.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    path = (
        f"/reporting/v2/tenant/{client.tenant_id}/report-category/{category}"
        f"/reports/{report_id}"
    )
    data = await client.get(path)
    return _fmt(data)


@mcp.tool()
async def get_report_parameter_values(tenant: str, dynamic_set_id: str) -> str:
    """Resolve a report parameter's allowed values (dynamic value set).

    When to use: a report parameter from `get_report` /
    `list_reports_in_category` has `acceptValues.dynamicSetId` set — that
    means its value is a code drawn from a named set, not free-form. Pass
    that id here to get the `[value, name]` pairs, then supply the chosen
    `value` to `run_report`.

    Example: `dynamic_set_id="job-date-filter-type"` →
      [[0,"Invoice Date"],[1,"Job Completion Date"], …] so a "Filter by" /
      DateType parameter can be set to 0, 1, … correctly.

    These sets are static within a session — resolve once and reuse the
    values across calls rather than re-fetching (reporting quota is tight).

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    path = f"/reporting/v2/tenant/{client.tenant_id}/dynamic-value-sets/{dynamic_set_id}"
    data = await client.get(path)
    return _fmt(data)


@mcp.tool()
async def run_report(
    tenant: str,
    report_id: int,
    category: str,
    parameters: dict | None = None,
    page: int = 1,
    page_size: int = 50,
) -> str:
    """Run a configured ServiceTitan report — LAST RESORT, slow + quota-constrained.

    When to use: pre-aggregated analytics that no typed tool can answer
    cheaply (e.g. total revenue by BU and month combined). Reports are
    the right call for exec dashboards.
    When NOT: if the question is "list X with filter Y", use the typed
    list_* tool — far faster and doesn't burn the 5/min reporting quota.

    Required flow:
      1. `list_report_categories` → pick a category (the slug goes in the
         `category` arg).
      2. `list_reports_in_category` → pick a report_id AND read its
         required parameters. (If you already know the report_id, call
         `get_report(category, report_id)` instead — it returns one
         report's schema directly.)
      3. For any parameter whose `acceptValues.dynamicSetId` is set (e.g. a
         "Filter by" / DateType code, or a Business Unit picker), call
         `get_report_parameter_values(dynamicSetId)` to get the legal value.
      4. Call this tool with parameters as a JSON object, e.g.
         `parameters={"DateType":0,"From":"2024-01-01","To":"2024-12-31","BusinessUnitIds":[1,2,3]}`

    Rate limits: reporting has its own bucket (~3 req/min). If you need
    multiple reports, space them out.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    if parameters is not None and not isinstance(parameters, dict):
        return "Error: 'parameters' must be a JSON object mapping report-param names to values."

    client = _resolve(tenant)

    param_map = parameters or {}
    body: dict = {
        "parameters": [
            {"name": name, "value": value} for name, value in param_map.items()
        ]
    }

    path = (
        f"/reporting/v2/tenant/{client.tenant_id}/report-category/{category}"
        f"/reports/{report_id}/data?page={page}&pageSize={page_size}"
    )
    data = await client.post(path, json_body=body)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  PAYROLL
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_payrolls(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
) -> str:
    """List payroll RUNS (each payroll cycle — pay period envelope).

    When to use: entry point to payroll data — find a payrollId, then
    drill into `list_employee_payrolls` or `list_gross_pay_items`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("payroll", "payrolls", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_employee_payrolls(
    tenant: str,
    payroll_id: int,
    page: int = 1,
    page_size: int = 200,
) -> str:
    """Per-employee summary for one payroll run (gross/net, hours, taxes).

    When to use: payroll review at the employee level for a specific
    period. Requires payroll_id from `list_payrolls`.
    When NOT: for the underlying line items (each shift, bonus, tip),
    use `list_gross_pay_items`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    path = f"/payroll/v2/tenant/{client.tenant_id}/payrolls/{payroll_id}/employee-payrolls"
    data = await client.get(path, params={"page": page, "pageSize": page_size})
    return _fmt(data)


@mcp.tool()
async def list_gross_pay_items(
    tenant: str,
    payroll_id: int,
    employee_id: int | None = None,
    page: int = 1,
    page_size: int = 200,
) -> str:
    """List individual gross-pay line items (shift, bonus, commission, tip).

    When to use: auditing one employee's earnings composition, spiff
    verification, commission calculation.
    Tip: always pass `employee_id` when investigating one person — reduces
    result size considerably.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {"page": page, "pageSize": page_size}
    if employee_id:
        params["employeeId"] = employee_id
    path = f"/payroll/v2/tenant/{client.tenant_id}/gross-pay-items"
    data = await client.get(path, params=params)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  TELECOM — Calls
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_calls(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    created_on_or_after: str | None = None,
    created_on_or_before: str | None = None,
) -> str:
    """List telecom calls (inbound/outbound phone records with recordings).

    When to use: call-center analysis, CSR QA, lead-to-call mapping,
    after-hours missed-call audits. Always pass a date range — call
    volume is typically high.
    When NOT: for the resulting booking, use `list_bookings`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if created_on_or_after:
        params["createdOnOrAfter"] = created_on_or_after
    if created_on_or_before:
        params["createdOnOrBefore"] = created_on_or_before
    data = await client.list_resource("telecom", "calls", page, page_size, params)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  FORMS
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_forms(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List form TEMPLATES (definitions: "Post-Install Checklist", etc.).

    When to use: form-config audit, resolving formId from a submission.
    When NOT: for actual completed forms, use `list_form_submissions`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("forms", "forms", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_form_submissions(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    created_on_or_after: str | None = None,
) -> str:
    """List completed form submissions (techs filling in checklists on jobs).

    When to use: QA review — "did the tech complete the post-install
    form", compliance audits. Filter by date to stay manageable.
    When NOT: for the template definition, use `list_forms`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if created_on_or_after:
        params["createdOnOrAfter"] = created_on_or_after
    data = await client.list_resource("forms", "submissions", page, page_size, params)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  MARKETING — Campaigns
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_campaigns(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List marketing campaigns (the source attribution for jobs/leads) — static config.

    When to use: resolving campaignId on a job or lead to the channel
    name, or enumerating what channels exist. Small and static — cache it.
    When NOT: for spend data, use `list_campaign_costs`.

    page_size is passed through to ServiceTitan. Trust the `hasMore=…` footer
    over the item count when deciding whether you've seen every campaign.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("marketing", "campaigns", page, page_size)
    return _fmt(data)


@mcp.tool()
async def list_campaign_costs(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    campaign_id: int | None = None,
) -> str:
    """List marketing campaign COSTS (spend per campaign per period).

    When to use: CPL / ROAS / marketing-efficiency questions. Pair with
    `list_invoices` filtered by campaign for revenue side.
    Always pass `campaign_id` when investigating one channel.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if campaign_id:
        params["campaignId"] = campaign_id
    data = await client.list_resource("marketing", "costs", page, page_size, params)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  EQUIPMENT SYSTEMS
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_installed_equipment(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    location_id: int | None = None,
) -> str:
    """List equipment installed at customer locations (serial numbers, install dates).

    When to use: "what equipment is at this customer's home", warranty
    lookups, replacement-age targeting. Always pass `location_id` when
    you have it.
    When NOT: for pricebook definitions (what we sell), use
    `list_pricebook_equipment`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if location_id:
        params["locationId"] = location_id
    data = await client.list_resource("equipment-systems", "installed-equipment", page, page_size, params)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  TASK MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_tasks(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
) -> str:
    """List internal tasks (the follow-up/todo module, not field jobs).

    When to use: office follow-up audit — "callbacks owed", "open
    complaints to resolve".
    When NOT: for actual field work, use `list_jobs`. These are separate
    entities with a separate lifecycle.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("task-management", "tasks", page, page_size)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  TIMESHEETS
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def list_activities(
    tenant: str,
    page: int = 1,
    page_size: int = 200,
    starts_on_or_after: str | None = None,
) -> str:
    """List tech timesheet activities (drive time, job time, breaks — actual clock).

    When to use: labor cost analysis, tech productivity (wrench time /
    drive time ratio), timesheet audits.
    When NOT: for the SCHEDULED shifts, use `list_technician_shifts`.
    For payroll-level aggregates, use `list_gross_pay_items`.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if starts_on_or_after:
        params["startsOnOrAfter"] = starts_on_or_after
    data = await client.list_resource("timesheets", "activities", page, page_size, params)
    return _fmt(data)


@mcp.tool()
async def list_activity_categories(tenant: str, page: int = 1, page_size: int = 200) -> str:
    """List timesheet activity categories (Drive, Job, Break, Training) — static config.

    When to use: resolving activityCategoryId to a human label.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    data = await client.list_resource("timesheets", "activity-categories", page, page_size)
    return _fmt(data)


# ═══════════════════════════════════════════════════════════════════════
#  BUNDLED LOOKUPS
# ═══════════════════════════════════════════════════════════════════════

# Static-config lookup tables, keyed by short kind name → (category, resource)
# tuples accepted by client.list_resource. Keep in sync with bullet #6 of the
# server instructions and the static-config tool docstrings.
_LOOKUP_KINDS: dict[str, tuple[str, str]] = {
    "business_units": ("settings", "business-units"),
    "job_types": ("jpm", "job-types"),
    "zones": ("dispatch", "zones"),
    "warehouses": ("inventory", "warehouses"),
    "payment_types": ("accounting", "payment-types"),
    "tax_zones": ("accounting", "tax-zones"),
    "payment_terms": ("accounting", "payment-terms"),
    "membership_types": ("memberships", "membership-types"),
    "tag_types": ("settings", "tag-types"),
    "activity_categories": ("timesheets", "activity-categories"),
    "pricebook_categories": ("pricebook", "categories"),
    "inventory_vendors": ("inventory", "vendors"),
    "trucks": ("inventory", "trucks"),
    "user_roles": ("settings", "user-roles"),
    "campaigns": ("marketing", "campaigns"),
}

# Per-(tenant, kind) cache for the lookup resource. These tables are static
# config — re-fetching them on every read defeats the whole point of exposing
# them as resources. Tests reach into this dict directly to assert cache
# behavior; keep its shape stable.
_RESOURCE_CACHE_TTL_SECONDS = 3600
_resource_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _resource_cache_get(tenant: str, kind: str) -> str | None:
    entry = _resource_cache.get((tenant, kind))
    if entry is None:
        return None
    expires_at, payload = entry
    if time.monotonic() >= expires_at:
        _resource_cache.pop((tenant, kind), None)
        return None
    return payload


def _resource_cache_put(tenant: str, kind: str, payload: str) -> None:
    _resource_cache[(tenant, kind)] = (
        time.monotonic() + _RESOURCE_CACHE_TTL_SECONDS,
        payload,
    )


@mcp.tool()
async def get_lookup_tables(
    tenant: str,
    kinds: list[str] | None = None,
    page_size: int = 200,
) -> str:
    """Fetch multiple static lookup tables in one round trip — static config.

    When to use: at the start of a session when you need to resolve IDs
    across several lookup tables (business unit, job type, zone, etc.).
    Replaces 5–10 sequential `list_*` calls with a single fan-out. Cache
    the result and reuse the IDs for the rest of the conversation — these
    tables rarely change.
    When NOT: when you only need one specific table — call the matching
    `list_*` tool directly. When you need filters (e.g. campaigns by date)
    — this tool always pulls the first `page_size` rows unfiltered.

    kinds: subset of {business_units, job_types, zones, warehouses,
        payment_types, tax_zones, payment_terms, membership_types,
        tag_types, activity_categories, pricebook_categories,
        inventory_vendors, trucks, user_roles, campaigns}. Defaults to
        the full set when omitted.

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    requested = list(kinds) if kinds else list(_LOOKUP_KINDS)
    unknown = [k for k in requested if k not in _LOOKUP_KINDS]
    if unknown:
        return json.dumps(
            {
                "error": f"unknown kinds: {unknown}",
                "valid_kinds": sorted(_LOOKUP_KINDS),
            },
            indent=2,
        )

    async def _fetch(kind: str) -> tuple[str, dict | Exception]:
        category, resource = _LOOKUP_KINDS[kind]
        try:
            data = await client.list_resource(category, resource, 1, page_size)
            return kind, data
        except Exception as exc:
            return kind, exc

    results = await asyncio.gather(*(_fetch(k) for k in requested))

    out: dict = {}
    for kind, data in results:
        if isinstance(data, Exception):
            out[kind] = {"error": f"{type(data).__name__}: {data}"}
            continue
        items = data.get("data", []) if isinstance(data, dict) else []
        total = data.get("totalCount") if isinstance(data, dict) else None
        out[kind] = {
            "count": len(items),
            "totalCount": total if total is not None else "unknown",
            "items": items,
        }
    out["_meta"] = {
        "kinds_fetched": requested,
        "note": (
            f"Bundled {len(requested)} lookup table(s) in one call. These "
            "tables are static config — cache these IDs for the rest of "
            "the conversation instead of re-fetching."
        ),
    }
    return json.dumps(out, indent=2, default=str)


@mcp.resource(
    "servicetitan://tenants",
    name="ServiceTitan tenants",
    description="Configured tenant names — names only, no IDs or secrets.",
    mime_type="application/json",
)
def resource_tenants() -> str:
    """Static blob of configured tenant names.

    Mirrors `list_tenants` so clients that auto-prefetch resources avoid
    spending the bootstrap tool call.
    """
    return json.dumps({"tenants": tenant_names()}, indent=2)


@mcp.resource(
    "servicetitan://lookups/{tenant}/{kind}",
    name="ServiceTitan static lookup table",
    description=(
        "One static-config lookup table for a tenant. `kind` is one of: "
        "business_units, job_types, zones, warehouses, payment_types, "
        "tax_zones, payment_terms, membership_types, tag_types, "
        "activity_categories, pricebook_categories, inventory_vendors, "
        "trucks, user_roles, campaigns. Cached server-side per (tenant, "
        "kind) for one hour."
    ),
    mime_type="application/json",
)
async def resource_lookup(tenant: str, kind: str) -> str:
    """Read one static lookup table for `tenant`.

    On cache hit, returns the cached JSON without touching the API. On
    miss, fetches via `client.list_resource(...)` with the same per-tenant
    rate-limit machinery the tools use, caches the result, and returns it.
    """
    tenant_key = tenant.strip().lower()
    if kind not in _LOOKUP_KINDS:
        return json.dumps(
            {
                "error": f"unknown kind: {kind!r}",
                "valid_kinds": sorted(_LOOKUP_KINDS),
            },
            indent=2,
        )

    cached = _resource_cache_get(tenant_key, kind)
    if cached is not None:
        return cached

    client = _resolve(tenant_key)
    category, resource = _LOOKUP_KINDS[kind]
    data = await client.list_resource(category, resource, 1, 200)
    items = data.get("data", []) if isinstance(data, dict) else []
    payload = json.dumps(
        {"tenant": tenant_key, "kind": kind, "items": items},
        indent=2,
        default=str,
    )
    _resource_cache_put(tenant_key, kind, payload)
    return payload


# ═══════════════════════════════════════════════════════════════════════
#  GENERIC / POWER-USER
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def servicetitan_api_call(
    tenant: str,
    method: str,
    path: str,
    query_params: dict | None = None,
    body: dict | list | None = None,
) -> str:
    """ESCAPE HATCH: raw HTTP call to any ServiceTitan API endpoint.

    When to use: the typed tools above don't cover what you need —
    unusual search params, POST/PATCH/PUT writes, or endpoints from the
    ST docs that aren't wrapped here (e.g. `/crm/v2/tenant/{tenant_id}/
    customers/{id}/contacts`, scheduling booking conversions, etc.).
    When NOT: for anything a typed tool already covers — typed tools
    validate params, return clean JSON, and the docstring tells the LLM
    when to use them. Reach for this only after checking.

    Args:
      method: GET | POST | PATCH | PUT
      path: full API path starting with /. Use the literal string
            `{tenant_id}` as a placeholder — it's substituted at runtime.
            Example: `/crm/v2/tenant/{tenant_id}/customers?phone=555-0100`
      query_params: JSON object of query params, e.g. `{"pageSize":100}`
      body: JSON object (or array) for POST/PATCH/PUT request body

    Still goes through the same rate-limit + retry layer as typed tools
    (including the reporting bucket for `/reporting/*` paths).

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    if query_params is not None and not isinstance(query_params, dict):
        return "Error: 'query_params' must be a JSON object."
    if body is not None and not isinstance(body, (dict, list)):
        return "Error: 'body' must be a JSON object or array."

    client = _resolve(tenant)
    resolved_path = path.replace("{tenant_id}", client.tenant_id)

    try:
        method_upper = method.upper()
        if method_upper == "GET":
            data = await client.get(resolved_path, params=query_params)
        elif method_upper == "POST":
            data = await client.post(resolved_path, json_body=body)
        elif method_upper == "PATCH":
            data = await client.patch(resolved_path, json_body=body)
        elif method_upper == "PUT":
            data = await client.put(resolved_path, json_body=body)
        else:
            return f"Unsupported method: {method}. Use GET, POST, PATCH, or PUT."
        return _fmt(data)
    except Exception as e:
        return f"API Error: {e}\n{traceback.format_exc()}"


# ═══════════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════

def main():
    """Run the MCP server via stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
