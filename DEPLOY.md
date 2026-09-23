# Deploying search-mcp

The service is a single stateless container: FastAPI REST + FastMCP (streamable HTTP at `/mcp`) + `/health`. Any host that runs a Dockerfile works.

## Recommendation: Render

**One-liner:** $7/mo flat for an always-on 512 MB container with Dockerfile support, free custom domain + automatic TLS, and GitHub auto-deploys — the cheapest predictable option for this workload.

## Comparison (verified Sept 2026)

| | Railway | Render | Fly.io |
|---|---|---|---|
| Always-on cost (1 small svc) | ~$10–15/mo: Hobby $5 minimum + metered compute (~$20/vCPU-mo, ~$10/GB RAM-mo, $0.05/GB egress) | **$7/mo** Starter (0.5 CPU / 512 MB), flat, prorated per second | ~$7–8/mo (shared-cpu-1x, 1 GB, billed per second) |
| Free tier | Trial: $5 credit for 30 days, then $1/mo credit — not prod-viable | $0 (512 MB, spins down after inactivity — fine for staging) | New accounts get a small credit; check current terms |
| Dockerfile support | Yes | Yes (BuildKit, zero-downtime deploys) | Yes (`fly deploy`) |
| Custom domain + TLS | Yes | Yes, free, automatic | Yes |
| Deploy friction | Lowest (dashboard / `git push`) | Low (dashboard or `render.yaml` blueprint — included in repo) | Medium (CLI-driven) |
| Watch out | Metered billing has no ceiling until you set replica limits | 512 MB is plenty for search/fetch (no heavy local deps) | Per-second billing; volumes cost extra if you add disk |

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `SEARCH_API_KEY` | unset | **Required.** Tavily API key (free 1,000 credits/mo at tavily.com). `sync: false` in `render.yaml` → Render prompts at deploy |
| `PORT` | `8000` | Injected automatically by Railway/Render/Fly — do not set manually |
| `HOST` | `0.0.0.0` | |
| `X402_ENABLED` | `false` | Per-call x402 payments (facilitator-verified). **Leave off until** `X402_PAY_TO` + `X402_FACILITATOR_URL` are set, CDP keys are in place for mainnet, and the AZ LLC is formed |
| `STRIPE_ENABLED` | `false` | Monthly subscriber plans. Requires `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` from the Stripe dashboard |
| `RATE_LIMIT` | `60/minute` | e.g. `600/hour` |
| `X402_PRICE_USDC` / `X402_PAY_TO` | `0.015` / unset | `X402_PRICE_USDC` is the fallback; per-tool defaults (`X402_PRICE_SEARCH_WEB_USDC=0.025`, `X402_PRICE_FETCH_URL_USDC=0.01`) are proposed — Scott's call |

No secrets are hardcoded in the image. `SEARCH_API_KEY` is supplied at deploy time.

## Deploy steps

### Render (recommended)
1. Sign in at render.com → **New → Blueprint**.
2. Connect the `Anon2052/search-mcp` repo (private — authorize the Render GitHub App).
3. Blueprint name: `search-mcp`. Review shows one web service (Starter).
4. **Deploy Blueprint** — when prompted, paste your Tavily `SEARCH_API_KEY`.
5. **Create Web Service** — deploys on every push to `main`.
6. Custom domain: **Settings → Custom Domains** (free, TLS automatic).

### Railway
1. railway.app → **New Project → Deploy from GitHub** → select the repo.
2. Railway auto-detects the Dockerfile. Set `SEARCH_API_KEY` in **Variables**.
3. **Settings → Networking → Generate Domain** (or add a custom domain).
4. Hobby plan ($5/mo minimum + usage) required for always-on.

### Fly.io
```bash
fly launch          # Dockerfile auto-detected; accept the defaults
fly secrets set SEARCH_API_KEY=tvly-...
fly deploy
```
`fly.toml` is generated for you. Custom domain: `fly certs add <yourdomain>`.

## Local verification

```bash
docker build -t search-mcp:prod .
docker run --rm -p 8000:8000 -e SEARCH_API_KEY=tvly-... search-mcp:prod
curl localhost:8000/health   # -> {"status":"ok", ...}
```
