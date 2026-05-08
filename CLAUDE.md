# CLAUDE.md

## At session start: check `next_steps.md`

`next_steps.md` is a per-user session-handoff file at the repo root. **It is gitignored** (it may reference personal paths, in-flight work, or private plan files), so on a fresh clone it will not exist — that's expected.

**First thing to do in a new session:**

1. Check whether `next_steps.md` exists at the repo root.
2. **If it exists with content under "Active work":** treat that as the user's intent for this session and pick up from there (follow any plan file it points to).
3. **If it exists but contains only the `(no pending work — start fresh)` sentinel:** ignore it and wait for instructions.
4. **If it does not exist:** offer to create one from this template, then wait for instructions. Don't populate it until the user has pending work worth handing off.

Empty template (initial state — no pending work):

```markdown
# Next Steps

_(no pending work — start fresh)_
```

Populated template (when there's work to hand off):

```markdown
# Next Steps

## Active work
<one paragraph describing the task, brainstorm, or plan to resume>

## Plan file (if any)
<absolute path to a plan file, or "none">

## Notes
<anything the next session needs that isn't in the plan file>
```

When a session ends:
- If there's pending work, offer to update `next_steps.md` with a short handoff.
- If the session's goal was fully achieved and nothing's pending, reset `next_steps.md` back to the empty sentinel.

## Project

Python MCP server wrapping ServiceTitan's REST API. Exposes ~60 tools across CRM, Jobs, Dispatch, Estimates, Invoicing, Pricebook, Inventory, Memberships, Payroll, Reporting, Forms, Marketing. Full tool catalog and user-facing setup live in [README.md](README.md).

## Stack & entry point

- Python 3.10+. Deps: `mcp[cli]`, `httpx[socks]`, `pydantic`.
- Console script: `servicetitan-mcp` → `servicetitan_mcp.server:main` (see [pyproject.toml](pyproject.toml)).
- Transport: stdio by default; `MCP_TRANSPORT=sse` for HTTP/SSE deployments.

## Layout

Four source files matter:

- [`servicetitan_mcp/auth.py`](servicetitan_mcp/auth.py) — `TokenManager` for OAuth2 Client Credentials. Tokens cached per `tenant_id`; auto-refreshed ~14m into a 15m lifetime.
- [`servicetitan_mcp/config.py`](servicetitan_mcp/config.py) — Tenant registry. Reads `ST_TENANTS` + namespaced per-tenant env vars into `TenantCredentials` records. Strict lowercase slug validation. Fails fast with a migration hint if the legacy single-tenant vars are set without `ST_TENANTS`.
- [`servicetitan_mcp/client.py`](servicetitan_mcp/client.py) — `ServiceTitanClient`, shared `httpx.AsyncClient`, per-tenant token buckets via `main_limiter_for(name)` / `reporting_limiter_for(name)` factories (main API + reporting API), process-wide concurrency semaphore, retry with exponential backoff honoring `Retry-After`.
- [`servicetitan_mcp/server.py`](servicetitan_mcp/server.py) — `FastMCP` instance + all `@mcp.tool()` handlers + the `_fmt()` pagination formatter + `_get_client(tenant)` / `_resolve(tenant)` factories + the `list_tenants` discovery tool.

Tests live in [`tests/`](tests/) (pytest-asyncio): token bucket, retry/concurrency, pagination.

## Commands

- Install: `pip install -e .`
- Run: `python -m servicetitan_mcp.server` (or the `servicetitan-mcp` script)
- Test: `pytest` (use `pytest tests/test_pagination.py` to target one file)
- No linter/formatter configured — don't invent one.

## Required env vars

Multi-tenant; configure one or more tenants:

- `ST_TENANTS` — comma-separated tenant slugs (lowercase, `^[a-z][a-z0-9_-]*$`).
- For each slug `<NAME>` (uppercase in env-var keys): `ST_TENANT_<NAME>_ID`, `ST_TENANT_<NAME>_CLIENT_ID`, `ST_TENANT_<NAME>_CLIENT_SECRET`, `ST_TENANT_<NAME>_APP_KEY`.

Legacy single-tenant vars (`ST_APP_KEY`, `ST_CLIENT_ID`, `ST_CLIENT_SECRET`, `ST_TENANT_ID`) are no longer read. If present without `ST_TENANTS`, startup raises a `RuntimeError` with a migration hint.

Optional tuning (defaults in parens): `ST_RATE_LIMIT_RPS` (30, per tenant), `ST_REPORTING_RPM` (3, per tenant), `ST_MAX_CONCURRENCY` (10, process-wide).

## Adding a new tool

Follow the existing pattern — `list_customers` in [server.py](servicetitan_mcp/server.py) is a clean reference:

```python
@mcp.tool()
async def list_<resource>(tenant: str, page: int = 1, page_size: int = 200, ...) -> str:
    """Short purpose. When to use: ... When NOT: ...

    tenant: name of a configured ServiceTitan tenant (call list_tenants)
    """
    client = _resolve(tenant)
    params: dict = {}
    if some_filter:
        params["someFilter"] = some_filter
    data = await client.list_resource("<category>", "<resource>", page, page_size, params)
    return _fmt(data)
```

- `tenant: str` is always the first parameter, and the docstring always ends with the `tenant: name of a configured ServiceTitan tenant (call list_tenants)` line. FastMCP derives the tool schema from the signature; the docstring line is how the LLM knows to call `list_tenants` first.
- Use `_resolve(tenant)` (not `_get_client` directly) — it translates `UnknownTenantError` into a friendly LLM-facing message listing the configured tenants.
- Use `client.list_resource()` / `client.get_resource()` — they handle auth, rate limits, retries, and pooling. Don't build your own request.
- Always return `_fmt(data)` for listings — it appends the pagination footer Claude needs to decide whether to fetch more.
- Include LLM-facing "when to use / when NOT" guidance in the docstring; existing tools model this.
- For non-standard endpoints: `client.get/post/patch/put`, or the `servicetitan_api_call` escape hatch for fully ad-hoc paths.

## Gotchas / invariants

- **Reporting API is aggressively throttled** (3 rpm vs 30 rps main). Reporting calls will block each other — don't parallelize them.
- **Pagination footer is load-bearing.** `_fmt()` appends `page/pageSize/totalCount/hasMore` (inferred when ST omits it). A silent 25-item cap bug was fixed in commit `169bce0`; the regression test is [`tests/test_pagination.py`](tests/test_pagination.py). Don't bypass `_fmt()`.
- **Errors are surfaced on purpose.** After final retry, `client.py` raises with the response body (first 2000 chars) so Claude can see the reason. Don't swallow exceptions in tool handlers — let them propagate to MCP.
- **Token refresh is automatic.** Tool code should never touch `TokenManager` directly.
- **Shared HTTP client.** Don't spin up new `httpx.AsyncClient` instances inside handlers — use `_resolve(tenant)`.
- **Per-tenant rate limiters.** `main_limiter_for(name)` / `reporting_limiter_for(name)` return per-tenant singletons. The process-wide concurrency semaphore is separate and is shared across tenants (it guards local fan-out, not ST quota).
- **No silent default tenant.** Never add a fallback that picks a tenant when `tenant` is absent. A silent wrong-tenant answer is worse than any UX inconvenience.

## Git remote

Single remote:

- `origin` → `github.com/jtron9k/servicetitan-mcp` (the user's fork). Push here.

The glassdoc upstream is not configured as a remote in this clone.

## Where to look next

- `next_steps.md` — session handoff (check first; gitignored, create from template above if missing)
- [`README.md`](README.md) — end-user setup and the full tool catalog
- [`claude_desktop_config_example.json`](claude_desktop_config_example.json) — Claude Desktop config template
