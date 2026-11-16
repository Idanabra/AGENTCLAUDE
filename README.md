# SAP Sales Cloud V2 connector for Claude

Ask Claude questions about your SAP Sales Cloud V2 data, in Hebrew or English, right in claude.ai:

> מי קנה מוצרים מקבוצת משאבות?
> Who bought products from group Pumps?

Claude answers with account, quote, date, product, quantity and unit, from your sales quotations.

The connector is a small read-only MCP server. It only sends **GET** requests to SAP.

## What's in the repo

| File | Purpose |
|---|---|
| `server.py` | MCP server for Claude, plus the settings page and its API |
| `index.html` | Settings page: SAP base URL, user and password, connection test, connector URL |
| `project_instructions.md` | Text to paste into your Claude Project |
| `Dockerfile`, `requirements.txt` | Deployment |
| `.env.example` | Environment variables |

## 1. Deploy

The server needs a public HTTPS address so claude.ai can reach it. Any Docker or Python host works: Railway, Render, Azure App Service, Google Cloud Run, or your own server.

Set one environment variable:

| Variable | Value |
|---|---|
| `MCP_TOKEN` | a long random secret: `python -c "import secrets;print(secrets.token_urlsafe(32))"` |

Mount a persistent volume on `/app/data` so the saved SAP settings survive redeploys. Without a volume, enter the SAP details as environment variables instead (`SAP_BASE_URL`, `SAP_USER`, `SAP_PASSWORD`).

Run locally:
```bash
pip install -r requirements.txt
cp .env.example .env      # set MCP_TOKEN
python server.py          # http://localhost:8000
```

## 2. Enter the SAP connection

Open `https://<your-host>/`:
1. Unlock with your `MCP_TOKEN`.
2. Enter the base URL (for example `https://my1001209.de1.demo.crm.cloud.sap`), user and password. **Test connection** checks every SAP service; **Save connection** stores it on the server.
3. Copy the connector URL.

The password is stored on the server (file permissions 600) and is never sent back to the page. The page is in English and Hebrew.

## 3. Connect Claude

1. claude.ai → Settings → Connectors → **Add custom connector** → paste the connector URL.
2. Create a **Project** and paste `project_instructions.md` into its instructions.
3. In a chat in the project, turn the connector on from the tools menu and ask away.

## Tools Claude gets

| Tool | Use |
|---|---|
| `who_bought` | Who bought a product group or product, from sales quote items, with totals per account |
| `find` | Find accounts, products, groups, territories or opportunities by name or code, Hebrew or English |
| `list_records` | Live OData query on any of the six services |
| `get_quote_items` | Products of one quote |
| `describe_fields` | Field names of an entity |
| `refresh_data` | Clear the cache (data is cached 60 minutes) |

## SAP services used (GET only)

```
/sap/c4c/api/v1/account-service/accounts
/sap/c4c/api/v1/sales-territory-service/salesTerritories
/sap/c4c/api/v1/sales-quote-service/salesQuotes
/sap/c4c/api/v1/sales-quote-service/salesQuotes/{id}/items
/sap/c4c/api/v1/product-service/products
/sap/c4c/api/v1/product-group-service/productGroups
/sap/c4c/api/v1/opportunity-service/opportunities
```

## Security

- Everything except `/health` needs `MCP_TOKEN`: the settings API through a header, the connector through `?token=` in its URL. Anyone with the connector URL can read the SAP data, so share it carefully.
- Use a technical SAP user with read-only authorization.
- Never commit `.env` or `data/` (both are in `.gitignore`).

## Note on GitHub Pages

`index.html` is served by `server.py`; it is not a standalone GitHub Pages site. A static page can't keep a password safe, and SAP doesn't accept browser calls from other sites, so the server handles every SAP request.
