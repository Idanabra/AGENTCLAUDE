"""
SAP Sales Cloud V2 - MCP server for Claude (claude.ai custom connector)
=======================================================================
Read-only (HTTP GET only) bridge between Claude chat and SAP Sales Cloud V2.

Settings page:            https://<your-host>/            (enter SAP base URL, user, password)
Endpoint for claude.ai:   https://<your-host>/mcp?token=<MCP_TOKEN>
Run locally:              python server.py        (listens on $PORT, default 8000)
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs

import httpx
import uvicorn
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

load_dotenv()

MCP_TOKEN = os.environ.get("MCP_TOKEN", "")          # secret for the connector URL and the settings page
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "./data/sap_config.json"))
ALLOW_HTTP = os.getenv("ALLOW_HTTP", "false").lower() == "true"   # only for local testing
PAGE_SIZE = int(os.getenv("SAP_PAGE_SIZE", "200"))
MAX_RECORDS = int(os.getenv("SAP_MAX_RECORDS", "20000"))
CACHE_TTL = float(os.getenv("CACHE_TTL_MINUTES", "60")) * 60
PORT = int(os.getenv("PORT", "8000"))

API = "/sap/c4c/api/v1"
ENTITY_PATHS = {
    "accounts":       f"{API}/account-service/accounts",
    "territories":    f"{API}/sales-territory-service/salesTerritories",
    "sales_quotes":   f"{API}/sales-quote-service/salesQuotes",
    "products":       f"{API}/product-service/products",
    "product_groups": f"{API}/product-group-service/productGroups",
    "opportunities":  f"{API}/opportunity-service/opportunities",
}
ITEMS_PATH = f"{API}/sales-quote-service/salesQuotes/{{id}}/items"
Entity = Literal["accounts", "territories", "sales_quotes", "products", "product_groups", "opportunities"]


# ---------------------------------------------------------------------------
# SAP access: GET only, paging, in-memory cache
# ---------------------------------------------------------------------------
class Settings:
    """SAP connection settings: saved from the settings page, else environment variables."""

    def __init__(self) -> None:
        self.base_url = os.getenv("SAP_BASE_URL", "").rstrip("/")
        self.user = os.getenv("SAP_USER", "")
        self.password = os.getenv("SAP_PASSWORD", "")
        self.source = "environment"
        self.updated_at: float | None = None
        if CONFIG_PATH.exists():
            try:
                d = json.loads(CONFIG_PATH.read_text())
                self.base_url, self.user, self.password = d["base_url"], d["user"], d["password"]
                self.updated_at, self.source = d.get("updated_at"), "settings page"
            except (ValueError, KeyError):
                pass

    @property
    def complete(self) -> bool:
        return bool(self.base_url and self.user and self.password)

    def save(self, base_url: str, user: str, password: str) -> None:
        self.base_url, self.user, self.password = base_url, user, password
        self.updated_at, self.source = time.time(), "settings page"
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"base_url": base_url, "user": user, "password": password,
                                   "updated_at": self.updated_at}))
        os.chmod(tmp, 0o600)
        tmp.replace(CONFIG_PATH)


def clean_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for suffix in ("/sap/c4c/api/v1", "/sap/c4c/api", "/sap/c4c", "/sap"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    ok = re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", url) or (
        ALLOW_HTTP and re.fullmatch(r"http://[A-Za-z0-9.-]+(:\d+)?", url))
    if not ok:
        raise ValueError("Base URL must look like https://myXXXXXX.crm.cloud.sap")
    return url


settings = Settings()


class SAP:
    def __init__(self) -> None:
        self.http: httpx.AsyncClient | None = None
        self.cache: dict[str, tuple[float, list[dict]]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.configure(settings.base_url, settings.user, settings.password)

    @staticmethod
    def make_client(base_url: str, user: str, password: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, auth=(user, password),
                                 headers={"Accept": "application/json"}, timeout=60)

    def configure(self, base_url: str, user: str, password: str) -> None:
        old = self.http
        self.http = self.make_client(base_url, user, password) if base_url and user and password else None
        self.cache.clear()
        if old is not None:
            try:
                asyncio.get_running_loop().create_task(old.aclose())
            except RuntimeError:   # no running loop (startup): nothing to close yet
                pass

    async def get(self, path: str, params: dict | None = None, client: httpx.AsyncClient | None = None) -> Any:
        client = client or self.http
        if client is None:
            raise RuntimeError("SAP connection is not set up yet. Open the connector's settings page "
                               "and enter the base URL, user and password.")
        allowed = tuple(ENTITY_PATHS.values()) + (f"{API}/sales-quote-service/salesQuotes/",)
        if not path.startswith(allowed):
            raise ValueError(f"Path not allowed: {path}")
        for attempt in range(3):
            try:
                r = await client.get(path, params=params)   # the only HTTP verb used
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise RuntimeError(f"Network error calling SAP: {e}") from e
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise RuntimeError("SAP returned 401 - user or password is wrong (check the settings page).")
            if r.status_code == 403:
                raise RuntimeError(f"SAP returned 403 for {path} - the technical user lacks authorization.")
            if r.is_error:
                raise RuntimeError(f"SAP HTTP {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError("SAP call failed")

    @staticmethod
    def rows(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for k in ("value", "results", "items", "data"):
                if isinstance(payload.get(k), list):
                    return payload[k]
        return []

    async def get_all(self, path: str, params: dict | None = None) -> list[dict]:
        out, seen, skip = [], set(), 0
        while len(out) < MAX_RECORDS:
            page = self.rows(await self.get(path, {**(params or {}), "$top": PAGE_SIZE, "$skip": skip}))
            new = 0
            for r in page:
                key = r.get("id") or json.dumps(r, sort_keys=True, default=str)
                if key not in seen:
                    seen.add(key); out.append(r); new += 1
            if len(page) < PAGE_SIZE or new == 0:
                break
            skip += PAGE_SIZE
        return out

    async def cached(self, key: str, loader) -> list[dict]:
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self.cache.get(key)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
            data = await loader()
            self.cache[key] = (time.time(), data)
            return data

    async def entity(self, name: str) -> list[dict]:
        return await self.cached(name, lambda: self.get_all(ENTITY_PATHS[name]))

    async def quote_items(self, quote: dict) -> list[dict]:
        async def load():
            rows = await self.get_all(ITEMS_PATH.format(id=quote["id"]))
            for r in rows:
                r["_quoteId"] = quote["id"]
            return rows
        return await self.cached(f"items:{quote['id']}", load)

    async def all_items(self) -> list[dict]:
        quotes = await self.entity("sales_quotes")
        sem = asyncio.Semaphore(8)

        async def one(q):
            async with sem:
                try:
                    return await self.quote_items(q)
                except Exception:  # noqa: BLE001 - skip a broken quote, keep the rest
                    return []
        results = await asyncio.gather(*(one(q) for q in quotes if q.get("id")))
        return [i for chunk in results for i in chunk]


sap = SAP()


# ---------------------------------------------------------------------------
# Helpers that tolerate unknown field names
# ---------------------------------------------------------------------------
def strings(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    """All (path, string) pairs inside a record."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += strings(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for v in obj:
            out += strings(v, prefix)
    elif obj is not None and not isinstance(obj, bool):
        out.append((prefix, str(obj)))
    return out


def first(rec: dict, *paths: str) -> Any:
    for p in paths:
        cur: Any = rec
        for part in p.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, "", [], {}):
            return cur
    return None


def name_of(rec: dict | None) -> str | None:
    if not isinstance(rec, dict):
        return None
    return first(rec, "formattedName", "name", "description", "displayName", "accountName",
                 "descriptions.0.description")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def matches(rec: dict, text: str) -> list[str]:
    t = norm(text)
    return [f"{p}={v}" for p, v in strings(rec) if t in norm(v)]


def slim(rec: dict, limit: int = 1500) -> dict:
    s = json.dumps(rec, ensure_ascii=False, default=str)
    return rec if len(s) <= limit else json.loads(json.dumps({k: rec[k] for k in list(rec)[:25]}, default=str))


def quantity_of(item: dict) -> tuple[Any, Any]:
    for key in ("quantity", "requestedQuantity", "orderQuantity", "confirmedQuantity"):
        v = item.get(key)
        if isinstance(v, dict):
            return v.get("content") or v.get("value"), v.get("uomCode") or v.get("unitCode") or v.get("uom")
        if v is not None:
            return v, item.get("quantityUnitCode") or item.get("unitCode") or item.get("uomCode")
    for p, v in strings(item):
        if "quantity" in p.lower() and re.fullmatch(r"-?\d+(\.\d+)?", v):
            return float(v), None
    return None, None


def product_ref(item: dict) -> set[str]:
    refs = set()
    for p in ("product.id", "product.displayId", "productId", "productDisplayId", "product"):
        v = first(item, p)
        if isinstance(v, (str, int)):
            refs.add(str(v))
    return refs


QUOTE_DATE_PATHS = ("documentDate", "postingDate", "quoteDate", "validFrom", "startDate",
                    "adminData.createdOn", "createdOn")


# ---------------------------------------------------------------------------
# MCP server and tools
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "SAP Sales Cloud V2",
    instructions=(
        "Read-only access to SAP Sales Cloud V2: accounts (clients), territories, sales quotes "
        "with items, products, product groups and opportunities. Questions may be in Hebrew or "
        "English: answer in the user's language. For 'who bought <product group/product>' use "
        "who_bought. Use find to locate a name before filtering. Never invent data."),
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    port=PORT,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def find(entity: Entity, text: str, limit: int = 20) -> dict:
    """Find records of one entity whose ANY text field contains `text` (case-insensitive,
    Hebrew or English, partial words OK). Returns matching records and which fields matched.
    Use it to resolve names of accounts, product groups, products, territories, opportunities."""
    data = await sap.entity(entity)
    hits = []
    for r in data:
        m = matches(r, text)
        if m:
            hits.append({"matched": m[:5], "record": slim(r)})
    return {"entity": entity, "searched": text, "total_matches": len(hits), "results": hits[:limit]}


@mcp.tool()
async def list_records(entity: Entity, filter: str | None = None, select: str | None = None,
                       orderby: str | None = None, top: int = 50, skip: int = 0) -> dict:
    """Live GET on one SAP entity with OData options, e.g. filter="status eq 'OPEN'",
    orderby="adminData/createdOn desc", select="id,displayId,formattedName". Max top 500."""
    params: dict[str, Any] = {"$top": min(top, 500), "$skip": skip}
    if filter: params["$filter"] = filter
    if select: params["$select"] = select
    if orderby: params["$orderby"] = orderby
    rows = SAP.rows(await sap.get(ENTITY_PATHS[entity], params))
    return {"entity": entity, "count": len(rows), "records": [slim(r) for r in rows]}


@mcp.tool()
async def get_quote_items(quote_id_or_number: str) -> dict:
    """Products (line items) of one sales quote, by its id or its displayed quote number."""
    quotes = await sap.entity("sales_quotes")
    q = next((x for x in quotes if quote_id_or_number in (str(x.get("id")), str(x.get("displayId")))), None)
    if not q:
        return {"error": f"Quote {quote_id_or_number} not found"}
    items = await sap.quote_items(q)
    return {"quote": slim(q, 800), "items": [slim(i) for i in items]}


@mcp.tool()
async def who_bought(product_group: str | None = None, product: str | None = None,
                     account: str | None = None, date_from: str | None = None,
                     date_to: str | None = None, max_lines: int = 300) -> dict:
    """Who bought a product group or product, based on SALES QUOTE ITEMS.
    product_group / product / account: name, code or ID, Hebrew or English, partial text OK.
    date_from / date_to: YYYY-MM-DD on the quote date.
    Returns one line per quote item: account, quote number, quote date, status, product,
    quantity, unit - plus totals per account."""
    if not product_group and not product:
        return {"error": "Give product_group or product."}
    products = await sap.entity("products")
    target: dict[str, dict] = {}
    notes: list[str] = []

    if product_group:
        groups = [g for g in await sap.entity("product_groups") if matches(g, product_group)]
        if not groups:
            return {"error": f"No product group matches '{product_group}'."}
        gkeys = {str(v) for g in groups for v in (g.get("id"), g.get("displayId")) if v}
        notes.append("Groups matched: " + ", ".join(f"{g.get('displayId')} {name_of(g) or ''}".strip() for g in groups))
        for p in products:
            linked = any(v in gkeys for path, v in strings(p)
                         if re.search(r"group|categor|hierarch", path, re.I))
            if linked:
                target[str(p.get("id"))] = p
        if not target:
            notes.append("No product references these groups via a group/category field; "
                         "check a product with find(entity='products', ...) to see the link.")
    if product:
        for p in products:
            if matches(p, product):
                target[str(p.get("id"))] = p
    if not target:
        return {"error": "No products found for the request.", "notes": notes}

    keys: dict[str, dict] = {}
    for p in target.values():
        for v in (p.get("id"), p.get("displayId")):
            if v:
                keys[str(v)] = p

    quotes = {q["id"]: q for q in await sap.entity("sales_quotes") if q.get("id")}
    accounts = {a.get("id"): a for a in await sap.entity("accounts")}
    lines, totals = [], {}
    for it in await sap.all_items():
        hit = next((keys[r] for r in product_ref(it) if r in keys), None)
        if not hit:
            continue
        q = quotes.get(it.get("_quoteId"), {})
        acc_ref = q.get("account") or q.get("soldToParty") or {}
        acc_id = acc_ref.get("id") if isinstance(acc_ref, dict) else acc_ref
        acc = accounts.get(acc_id) or (acc_ref if isinstance(acc_ref, dict) else {})
        acc_name = name_of(acc) or str(acc_id)
        if account and not matches({"n": acc_name, "id": acc_id, "d": acc.get("displayId")}, account):
            continue
        date = str(first(q, *QUOTE_DATE_PATHS) or "")[:10]
        if (date_from and date and date < date_from) or (date_to and date and date > date_to):
            continue
        qty, uom = quantity_of(it)
        lines.append({
            "account": acc_name, "account_id": acc.get("displayId") or acc_id,
            "quote": q.get("displayId") or q.get("id"), "quote_date": date,
            "quote_status": first(q, "status", "lifeCycleStatus", "statusDescription"),
            "product": first(it, "product.description", "description", "productDescription") or name_of(hit),
            "product_id": hit.get("displayId") or hit.get("id"),
            "quantity": qty, "unit": uom,
        })
        t = totals.setdefault(acc_name, {"quote_lines": 0, "quantity": 0.0})
        t["quote_lines"] += 1
        try:
            t["quantity"] += float(qty or 0)
        except (TypeError, ValueError):
            pass

    lines.sort(key=lambda r: r["quote_date"], reverse=True)
    return {"source": "sales quote items", "products_in_scope": len(target),
            "line_count": len(lines), "lines": lines[:max_lines],
            "totals_per_account": totals, "notes": notes}


@mcp.tool()
async def describe_fields(entity: Literal["accounts", "territories", "sales_quotes", "products",
                                          "product_groups", "opportunities", "sales_quote_items"]) -> dict:
    """Show the field structure of one sample record (to understand names before filtering)."""
    if entity == "sales_quote_items":
        quotes = await sap.entity("sales_quotes")
        rec = next((i for q in quotes[:5] for i in await sap.quote_items(q)), None)
    else:
        rows = SAP.rows(await sap.get(ENTITY_PATHS[entity], {"$top": 1}))
        rec = rows[0] if rows else None
    return {"entity": entity, "fields": [p for p, _ in strings(rec)] if rec else [], "sample": rec}


@mcp.tool()
async def refresh_data() -> dict:
    """Clear the cache so the next question reads fresh data from SAP."""
    n = len(sap.cache)
    sap.cache.clear()
    return {"cleared_entries": n}


# ---------------------------------------------------------------------------
# HTTP app with a shared-secret check
# ---------------------------------------------------------------------------
TEST_ENTITIES = {
    "accounts": "Accounts", "territories": "Territories", "sales_quotes": "Sales quotes",
    "products": "Products", "product_groups": "Product groups", "opportunities": "Opportunities",
}


async def test_connection(base_url: str, user: str, password: str) -> dict:
    """GET one record from every service with the given credentials."""
    client = SAP.make_client(base_url, user, password)
    results = []
    try:
        for key, label in TEST_ENTITIES.items():
            try:
                rows = SAP.rows(await sap.get(ENTITY_PATHS[key], {"$top": 1}, client=client))
                results.append({"entity": key, "label": label, "ok": True,
                                "fields": len(rows[0]) if rows else 0, "empty": not rows})
                if key == "sales_quotes" and rows and rows[0].get("id"):
                    items = SAP.rows(await sap.get(ITEMS_PATH.format(id=rows[0]["id"]), {"$top": 1}, client=client))
                    results.append({"entity": "sales_quote_items", "label": "Quote items", "ok": True,
                                    "fields": len(items[0]) if items else 0, "empty": not items})
            except Exception as e:  # noqa: BLE001
                results.append({"entity": key, "label": label, "ok": False, "error": str(e)[:300]})
    finally:
        await client.aclose()
    return {"ok": all(r["ok"] for r in results), "results": results}


def admin_ok(request: Request) -> bool:
    given = request.headers.get("x-admin-token", "")
    return bool(MCP_TOKEN) and hmac.compare_digest(given.encode(), MCP_TOKEN.encode())


async def denied() -> JSONResponse:
    await asyncio.sleep(1)   # slows down guessing
    if not MCP_TOKEN:
        return JSONResponse({"error": "MCP_TOKEN is not set on the server."}, status_code=503)
    return JSONResponse({"error": "Wrong access token."}, status_code=401)


def public_config() -> dict:
    return {"base_url": settings.base_url, "user": settings.user,
            "password_set": bool(settings.password), "source": settings.source,
            "updated_at": settings.updated_at, "complete": settings.complete}


async def read_body(request: Request) -> tuple[str, str, str]:
    body = await request.json()
    base_url = clean_base_url(body.get("base_url", ""))
    user = str(body.get("user", "")).strip()
    password = str(body.get("password", "")) or settings.password   # blank = keep the saved one
    if not user or not password:
        raise ValueError("User and password are required.")
    return base_url, user, password


async def api_config(request: Request) -> JSONResponse:
    if not admin_ok(request):
        return await denied()
    if request.method == "GET":
        return JSONResponse(public_config())
    try:
        base_url, user, password = await read_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    test = await test_connection(base_url, user, password)
    if not any(r["ok"] for r in test["results"]):
        return JSONResponse({"error": "Not saved: SAP did not accept this connection.", "test": test},
                            status_code=400)
    settings.save(base_url, user, password)
    sap.configure(base_url, user, password)
    return JSONResponse({"saved": True, "config": public_config(), "test": test})


async def api_test(request: Request) -> JSONResponse:
    if not admin_ok(request):
        return await denied()
    try:
        base_url, user, password = await read_body(request)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(await test_connection(base_url, user, password))


async def index(_: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "sap_configured": settings.complete})


STATIC_DIR = Path(__file__).parent
admin_app = Starlette(routes=[
    Route("/", index),
    Route("/index.html", index),
    Route("/health", health),
    Route("/api/config", api_config, methods=["GET", "POST"]),
    Route("/api/test", api_test, methods=["POST"]),
])
mcp_app = mcp.streamable_http_app()


async def app(scope, receive, send):
    """/mcp* -> MCP (needs ?token=), everything else -> settings page and its API."""
    if scope["type"] == "lifespan":
        return await mcp_app(scope, receive, send)
    if scope["type"] == "http" and scope["path"].startswith("/mcp"):
        token = parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0]
        if not MCP_TOKEN or not hmac.compare_digest(token.encode(), MCP_TOKEN.encode()):
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        return await mcp_app(scope, receive, send)
    return await admin_app(scope, receive, send)


if __name__ == "__main__":
    if not MCP_TOKEN:
        raise SystemExit("Set MCP_TOKEN (a long random secret) - see .env.example")
    if not settings.complete:
        print("SAP connection not set yet: open http://localhost:%d/ to enter it." % PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
