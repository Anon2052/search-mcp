# search-mcp

Web search + URL fetch MCP server (streamable HTTP) + REST API. Connector #2 in the monetized connector portfolio.

## Tools

| Tool | What it does |
|---|---|
| `search_web(query, count=5)` | Web search via Tavily (1,000 free credits/mo). Returns `[{title, url, snippet}]`. Count clamped to 1–10. |
| `fetch_url(url)` | Fetch a public http(s) URL, return main text content. SSRF-guarded: private/loopback/link-local/metadata IPs rejected, redirects re-validated. |

## Endpoints

- `GET /health` → `{"status","version","x402"}`
- `POST /search` → `{"query","count"}` → `{"results":[...]}`
- `POST /fetch` → `{"url"}` → `{"text"}`
- `POST /mcp` → MCP over streamable HTTP (stateless)

60/min per-IP rate limit (`RATE_LIMIT`). x402 micropayment metering is a **structural stub** behind `X402_ENABLED=false` — no on-chain verification.

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -e .
export SEARCH_API_KEY=tvly-...   # get a free key at https://tavily.com
uvicorn search_mcp.rest_app:app --host 0.0.0.0 --port 8000
curl localhost:8000/health
```

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `SEARCH_API_KEY` | unset | **Required** for search. Never commit it. |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | `PORT` injected by the host |
| `X402_ENABLED` | `false` | **Leave off** — stub, not prod-ready |
| `RATE_LIMIT` | `60/minute` | e.g. `600/hour` |
| `X402_PRICE_USDC` / `X402_PAY_TO` | `0.015` / unset | Only relevant when x402 verification is real |

## Deploy

See [DEPLOY.md](DEPLOY.md). One-click: `render.yaml` blueprint (Starter plan) — Render prompts for `SEARCH_API_KEY` at deploy time.
