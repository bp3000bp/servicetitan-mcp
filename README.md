# ServiceTitan MCP Server

Connect Claude to ServiceTitan via API. 60 tools covering CRM, Jobs, Accounting, Estimates, Dispatch, Pricebook, Inventory, Memberships, Payroll, Marketing, Reporting, Timesheets, Telecom, and more.

Multi-tenant: one server instance can query any number of ServiceTitan tenants on a per-call basis, including cross-tenant aggregation.

## Credits

This project is a fork of [glassdoc/servicetitan-mcp](https://github.com/glassdoc/servicetitan-mcp), created by the [glassdoc](https://github.com/glassdoc) team. All credit for the original design and the bulk of the implementation goes to them. This fork adds fixes for production auth, improved error surfacing, a corrected `run_report` endpoint, and multi-tenant support.

## Quick Start

```bash
# Clone and install
git clone https://github.com/glassdoc/servicetitan-mcp.git
cd servicetitan-mcp
pip install -e .
```

## Configuration

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, or `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "servicetitan": {
      "command": "python",
      "args": ["-m", "servicetitan_mcp.server"],
      "env": {
        "ST_TENANTS": "acme,other",

        "ST_TENANT_ACME_ID": "your-tenant-id",
        "ST_TENANT_ACME_CLIENT_ID": "cid.your-client-id",
        "ST_TENANT_ACME_CLIENT_SECRET": "cs1.your-client-secret",
        "ST_TENANT_ACME_APP_KEY": "your-app-key",

        "ST_TENANT_OTHER_ID": "your-tenant-id",
        "ST_TENANT_OTHER_CLIENT_ID": "cid.your-client-id",
        "ST_TENANT_OTHER_CLIENT_SECRET": "cs1.your-client-secret",
        "ST_TENANT_OTHER_APP_KEY": "your-app-key"
      }
    }
  }
}
```

### Env var schema

- `ST_TENANTS` — comma-separated list of tenant names. Names must be lowercase and match `^[a-z][a-z0-9_-]*$`. These are the names the LLM uses in tool calls, so pick slugs that are recognizable when phrased naturally ("st_louis" rather than "t1").
- For each name in `ST_TENANTS`, set four namespaced vars where `<NAME>` is the uppercase form of the slug:
  - `ST_TENANT_<NAME>_ID` — numeric ServiceTitan tenant id (used in API URLs).
  - `ST_TENANT_<NAME>_CLIENT_ID` — OAuth2 Client ID.
  - `ST_TENANT_<NAME>_CLIENT_SECRET` — OAuth2 Client Secret.
  - `ST_TENANT_<NAME>_APP_KEY` — `ST-App-Key` header value from the Developer Portal.

If one ServiceTitan app is authorized against multiple tenants, the `CLIENT_ID` / `CLIENT_SECRET` / `APP_KEY` will be identical across tenants and only `ID` differs. If each tenant has its own app registration, all four vary. The schema supports both.

For a complete ready-to-edit example, see [`claude_desktop_config_example.json`](claude_desktop_config_example.json).

## Getting Your Credentials

For each tenant:

1. Go to **Settings > Integrations > API Application Access** in that ServiceTitan tenant.
2. Find the **Claude MCP Integration** app and click **Connect**.
3. Fill in the restriction fields (booking_provider, gps_provider, report_category).
4. Accept Terms and Conditions.
5. Copy your **Client ID** and generate a **Client Secret**.
6. Your **Tenant ID** is shown in the top-right corner of any ServiceTitan page.
7. Your **App Key** is provided by ServiceTitan when your integration app is registered — obtain it directly from your own ServiceTitan account/integration setup.

## Multi-Tenant Usage

### The `tenant` argument

Every tool takes a required `tenant: str` as its first argument naming one of the configured tenants. There is no default — calling any tool without `tenant` raises a schema error from FastMCP. This is intentional: a silent wrong-tenant answer ("most recent job" on the wrong business) is worse than any UX inconvenience.

### Discovery: `list_tenants`

A new no-arg tool, `list_tenants`, returns the configured tenant names. The LLM typically calls this first when the user names a business by name, so it can map phrasing → tenant slug correctly.

### Cross-tenant queries

The server itself is always single-tenant per call. Cross-tenant aggregation is LLM-orchestrated: when the user asks something like *"total jobs booked across all businesses last month"*, Claude fans out — one tool call per tenant — and aggregates the results in its reply. No server-side join, no cross-tenant auth, no shared rate-limiter contention.

Practically, this means:

- **Per-tenant rate limiting is independent.** The 30 rps main / 3 rpm reporting limits apply *per tenant*, so four tenants have 4× the headroom of a single tenant in burst scenarios.
- **OAuth tokens cache per tenant.** The `TokenManager` is keyed on `tenant_id`, so sequential cross-tenant use does not invalidate any one tenant's token.
- **Concurrent cross-tenant fan-out works.** The process-wide concurrency semaphore (`ST_MAX_CONCURRENCY`, default 10) caps total parallelism, not per-tenant.

Example flows:

- *"Most recent job in St. Louis"* → single `list_jobs(tenant="st_louis", ...)` call.
- *"Show me revenue by business last month"* → Claude calls `list_invoices(tenant="...", created_on_or_after=...)` once per configured tenant and sums.

### Unknown tenant errors

Calling a tool with a tenant slug that isn't configured returns a structured error listing the valid names:

```
Unknown tenant 'foo'. Configured: acme, other. Call list_tenants for the authoritative list.
```

## Migrating from single-tenant

If you're upgrading from a pre-multi-tenant version of this server, your previous env vars (`ST_APP_KEY`, `ST_CLIENT_ID`, `ST_CLIENT_SECRET`, `ST_TENANT_ID`) are no longer read. On startup with the old config you'll see:

```
RuntimeError: ST_TENANTS is not set, but legacy single-tenant vars are
present: ST_TENANT_ID, ST_APP_KEY, ST_CLIENT_ID, ST_CLIENT_SECRET. This
server is now multi-tenant. Set ST_TENANTS=<comma-separated names> and
namespaced vars ST_TENANT_<NAME>_ID / _CLIENT_ID / _CLIENT_SECRET /
_APP_KEY per tenant. See README for the full migration.
```

Migration takes ~5 minutes per tenant: rename the four old vars with an `ST_TENANT_<NAME>_` prefix, and add `ST_TENANTS=<name>` naming that tenant. To add additional tenants, repeat the four-var block with new names and extend `ST_TENANTS`.

## Available Tools (72 + `list_tenants`)

Every tool below takes `tenant: str` as its first required argument. See "Multi-Tenant Usage" above.

### CRM
- `list_customers` — Search and list customers
- `get_customer` — Get customer details by ID
- `list_locations` — List service locations
- `get_location` — Get location details
- `list_leads` — List leads/opportunities
- `get_lead` — Get lead details by ID
- `list_bookings` — List bookings
- `get_booking` — Get booking details by ID
- `list_contacts` — List customer contacts

### Job Planning & Management
- `list_jobs` — List jobs with filters (status, customer, date range)
- `get_job` — Get full job details
- `list_appointments` — List appointments by date range
- `get_appointment` — Get appointment details by ID
- `list_job_types` — List configured job types
- `get_job_type` — Get one job-type definition by ID
- `list_projects` — List projects
- `get_project` — Get project details

### Accounting
- `list_invoices` — List invoices (filter by job, customer, date)
- `get_invoice` — Get invoice with line items
- `list_payments` — List payments received
- `list_payment_types` — List payment types
- `list_inventory_bills` — List AP/inventory bills
- `list_journal_entries` — List journal entries
- `list_payment_terms` — List payment terms
- `list_tax_zones` — List tax zones

### Sales & Estimates
- `list_estimates` — List estimates (filter by job, status, sold date)
- `get_estimate` — Get estimate with line items

### Dispatch
- `list_appointment_assignments` — Technician-to-appointment assignments
- `list_technician_shifts` — Shift schedules
- `get_technician_shift` — Get one shift by ID
- `list_zones` — Dispatch zones
- `list_non_job_appointments` — Meetings, training, etc.
- `get_non_job_appointment` — Get one non-job appointment by ID

### Pricebook
- `list_pricebook_services` — Sellable services
- `list_pricebook_materials` — Materials
- `list_pricebook_equipment` — Equipment
- `list_pricebook_categories` — Categories

### Inventory
- `list_purchase_orders` — Purchase orders
- `get_purchase_order` — Get one purchase order (with line items) by ID
- `list_warehouses` — Warehouses
- `list_inventory_vendors` — Vendors/suppliers
- `list_trucks` — Vehicle inventory

### Memberships
- `list_memberships` — Customer memberships
- `get_membership` — Get one customer membership by ID
- `list_membership_types` — Membership type definitions
- `list_recurring_services` — Recurring services
- `get_recurring_service` — Get one recurring service by ID

### Settings
- `list_tenants` — Configured tenant names (no `tenant` arg)
- `list_employees` — Employees
- `get_employee` — Employee details
- `list_technicians` — Technicians
- `get_technician` — Get one technician by ID
- `list_business_units` — Business units
- `list_tag_types` — Tag types
- `list_user_roles` — User roles

### Reporting
- `list_report_categories` — Report categories
- `list_reports_in_category` — Reports in a category (by category slug)
- `get_report` — One report's parameter schema + output fields (by category slug + id)
- `get_report_parameter_values` — Resolve a parameter's allowed values (dynamic value set)
- `run_report` — Run a dynamic report (returns rows inline; for small/interactive pulls)
- `run_report_to_file` — Auto-paginate a whole report and write it to a CSV/JSONL file, returning only metadata (for reports too large to return inline)

### Payroll
- `list_payrolls` — Payroll runs
- `list_employee_payrolls` — Employee payroll details
- `list_gross_pay_items` — Gross pay line items

### Telecom
- `list_calls` — Phone calls
- `get_call` — Get one call (with recording links) by ID

### Forms
- `list_forms` — Form templates
- `list_form_submissions` — Submitted forms

### Marketing
- `list_campaigns` — Marketing campaigns
- `list_campaign_costs` — Campaign costs

### Equipment, Tasks, Timesheets
- `list_installed_equipment` — Installed equipment at locations
- `list_tasks` — Task management tasks
- `list_activities` — Timesheet activities
- `list_activity_categories` — Activity categories

### Power User
- `export_feed` — Bulk-export an entire feed (customers, jobs, invoices, …) via continuation tokens; cheap on quota for full-table syncs and incremental change pulls
- `servicetitan_api_call` — Make any arbitrary API call (GET/POST/PATCH/PUT)

## Authentication

Uses OAuth 2.0 Client Credentials flow. Tokens auto-refresh every 15 minutes. Tokens cache per `tenant_id`, so cross-tenant use does not invalidate each other. No user interaction required.

## Common Workflows

Recipes for frequent questions, with the tool sequence Claude should pick. Every tool takes `tenant` as the first argument — omitted below for brevity, but always required.

### Find a customer by name (then drill down)
1. `list_customers(tenant=..., name="Smith")` → returns candidates with IDs, contact blocks, primary location.
2. Optional `get_customer(tenant=..., customer_id=...)` only if you need custom fields not in the list response.

### Find a customer by phone or email
The `list_customers` endpoint has no phone/email filter. Two options:
- `servicetitan_api_call(tenant=..., method="GET", path="/crm/v2/tenant/{tenant_id}/customers", query_params='{"phone":"555-0100"}')` — works if ST exposes `phone` on this tenant.
- Fallback: `list_contacts` with date range and match locally.

### Full job timeline for a customer
1. `list_customers(tenant=..., name=...)` → customerId
2. `list_jobs(tenant=..., customer_id=...)` → jobIds (status, dates)
3. For a specific job: `list_appointments(tenant=..., starts_on_or_after=...)` filtered to that job, or `get_job(tenant=..., job_id=...)` for full detail
4. `list_invoices(tenant=..., job_id=...)` for billing side
5. `list_estimates(tenant=..., job_id=...)` if a quote exists

### Revenue for a date range
- Simple total: `list_invoices(tenant=..., created_on_or_after="2024-01-01")` — sum client-side.
- Broken down by Business Unit or campaign: use `run_report` (reporting quota applies, slower). Discover the report first via `list_report_categories` → `list_reports_in_category(category)` (the category arg is the string slug, e.g. `"operations"`), or jump straight to `get_report(category, report_id)` if you already know the id. Then resolve any parameter that has an `acceptValues.dynamicSetId` (e.g. a "Filter by"/DateType code) with `get_report_parameter_values(dynamicSetId)`, and finally call `run_report`.

### Cross-tenant aggregation
- *"Revenue across all businesses last month"* → call `list_invoices` once per tenant from `list_tenants`, sum client-side.
- *"Which business has the most open estimates right now?"* → call `list_estimates(status="Open")` once per tenant, compare counts.

Claude orchestrates the fan-out; there's no server-side aggregation tool and no need to call `list_tenants` in the query path if the tenant set is already known.

### Technician's schedule today
1. `list_appointment_assignments(tenant=..., starts_on_or_after="2024-04-22", starts_on_or_before="2024-04-23")`
2. Cross-reference with `list_technicians(tenant=...)` (cache this — small and static) to map IDs → names.

### Open estimates by business unit
1. `list_business_units(tenant=...)` — cache the BU ID → name map.
2. `list_estimates(tenant=..., status="Open")` — note `businessUnitId` per estimate, group client-side.

### Membership renewals due this month
1. `list_memberships(tenant=..., status="Active")` — look at `nextScheduledDate` / `to` expiry.
2. For the tune-up visits themselves: `list_recurring_services(tenant=...)`.

### Marketing channel ROI
1. `list_campaigns(tenant=...)` — cache campaign ID → name.
2. `list_campaign_costs(tenant=..., campaign_id=...)` for spend.
3. `list_invoices(tenant=..., created_on_or_after=...)` then group by each job's campaignId for revenue attribution.

## Rate limits and error handling

ServiceTitan enforces **per app per tenant**:
- **Main API:** 60 requests/second.
- **Reporting API:** 5 requests/minute per report.

This server layers the following in front of those limits:

- **Per-tenant token buckets.** Each tenant gets its own pair (main + reporting) — they don't share quota. Four tenants have 4× the aggregate burst headroom of a single tenant. Defaults: `30 rps` main, `3 rpm` reporting (half the hard caps, leaving room for bursts). Override via:
  - `ST_RATE_LIMIT_RPS` (default `30`)
  - `ST_REPORTING_RPM` (default `3`)
  - `ST_MAX_CONCURRENCY` (default `10`) — process-wide cap on in-flight requests across all tenants
  - `ST_OUTPUTS_DIR` (optional) — default directory for `run_report_to_file` output; falls back to the in-repo `report_exports/` if unset. Must be readable by whatever consumes the file (e.g. your sandbox/file tools).
- **Automatic retry** on `429`, `502`, `503`, `504`:
  - Honors `Retry-After` header if the server sends one.
  - Otherwise exponential backoff: 1s → 2s → 4s, max 3 retries.
  - On final failure, the HTTP error (including response body) is raised so Claude sees what went wrong.
- **Shared connection pool**: one `httpx.AsyncClient` is reused across all tool calls and all tenants to avoid per-request TCP handshakes. Pooling is per-host; ST is one host, so splitting per tenant would gain nothing.

Retries log to stderr at the `[servicetitan-mcp]` prefix — tail the Claude Desktop logs to observe backoff behavior when investigating slowness.

## License

MIT
