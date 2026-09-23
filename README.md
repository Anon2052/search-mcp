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

60/min per-IP rate limit (`RATE_LIMIT`). Per-call payments via x402 (USDC on Base, facilitator-verified) and monthly plans via Stripe — **both OFF by default** (`X402_ENABLED=false`, `STRIPE_ENABLED=false`).

## Pricing (proposed — Scott's call)

| Tool / plan | Price | Mechanism |
|---|---|---|
| `search_web` | $0.025/call | x402 USDC on Base |
| `fetch_url` | $0.01/call | x402 USDC on Base |
| Pro Monthly | $9/mo | Stripe subscription → subscriber key bypasses per-call charges (fair use) |

Per-tool prices are baked in as defaults but **overridable without a deploy**: `X402_PRICE_<TOOLNAME>_USDC` (e.g. `X402_PRICE_SEARCH_WEB_USDC=0.05`). `X402_PRICE_USDC` (default `0.015`) is the fallback for unmapped calls.

MCP discovery (`initialize`, `tools/list`, `ping`, notifications) stays free so directory scanners keep working.

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
| `X402_ENABLED` | `false` | Per-call x402 payments. **Leave off until** `X402_PAY_TO` + `X402_FACILITATOR_URL` are set, CDP keys are in place for mainnet, and the AZ LLC is formed |
| `X402_PAY_TO` | unset | Your Base wallet address receiving USDC |
| `X402_FACILITATOR_URL` | `https://x402.org/facilitator` | **Base Sepolia testnet only.** For Base mainnet: `https://api.cdp.coinbase.com/platform/v2/x402` + `CDP_API_KEY_ID`/`CDP_API_KEY_SECRET` |
| `X402_FACILITATOR_API_KEY` | unset | Optional: `X-API-Key` auth for facilitators that use it |
| `X402_PRICE_SEARCH_WEB_USDC` / `X402_PRICE_FETCH_URL_USDC` | `0.025` / `0.01` | Proposed defaults — Scott's call |
| `STRIPE_ENABLED` | `false` | Monthly subscriber plans. Requires `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` (create the product/price in the Stripe dashboard) |
| `STRIPE_SUCCESS_URL` / `STRIPE_CANCEL_URL` | unset | Checkout redirect URLs (success URL should include `?session_id={CHECKOUT_SESSION_ID}`) |
| `BILLING_DB_PATH` | `/var/lib/search-mcp/billing.db` | SQLite subscriber store (only key hashes are stored). Must sit on the Render persistent disk (`render.yaml` `disk:` stanza); when developing locally, set this to `./data/subscribers.db` since `/var/lib` usually isn't writable |
| `SUBSCRIBER_MONTHLY_CALL_LIMIT` | `1000` | Pro fair-use cap: max calls per subscriber key per calendar month (UTC). Over the cap → HTTP 429 (x402 per-call still works). At ~$0.02 avg per-call, $9/mo ≈ 450 calls break-even — Scott's call on the number |
| `RATE_LIMIT` | `60/minute` | e.g. `600/hour` |

## Billing endpoints

- `POST /billing/checkout` → `{"checkout_url": ...}` (Stripe Checkout, subscription mode)
- `POST /billing/stripe-webhook` → Stripe event receiver (activates/deactivates subscribers)
- `GET /billing/key?session_id=...` → exchange a completed checkout for a subscriber key
- `GET /billing/status` → `{"stripe_enabled","x402_enabled"}` (no secrets)

Subscribers send `X-Subscriber-Key: <key>` instead of `X-Payment`.
Subscriber-key calls count against the monthly fair-use cap (`SUBSCRIBER_MONTHLY_CALL_LIMIT`, default 1000/month UTC); over the cap returns HTTP 429 with `limit`, `used`, and `resets_at` — paying per call via x402 still works.

## Deploy

See [DEPLOY.md](DEPLOY.md). One-click: `render.yaml` blueprint (Starter plan) — Render prompts for `SEARCH_API_KEY` at deploy time.
