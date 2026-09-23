"""Combined ASGI app: REST API + FastMCP (streamable HTTP) + /health.

Run with:  uvicorn search_mcp.rest_app:app --host 0.0.0.0 --port 8000

Requires SEARCH_API_KEY (Tavily) for /search and the search_web MCP tool.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from . import __version__
from . import fetch as _fetch
from . import search as _search
from .billing import router as billing_router
from .billing import stripe_effective_enabled
from .fetch import FetchError
from .mcp_server import mcp
from .search import SearchError
from .x402 import X402PaymentMiddleware, warn_if_payments_live, x402_effective_enabled

log = logging.getLogger("search_mcp")

DEFAULT_RATE_LIMIT = os.environ.get("RATE_LIMIT", "60/minute")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    count: int = Field(default=5, ge=1, le=10)


class FetchRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


def create_app(rate_limit: str | None = None) -> FastAPI:
    # MCP sub-app first: its lifespan must be handed to the parent FastAPI app,
    # otherwise FastMCP's session task group never initializes and every
    # request 500s ("Task group is not initialized"). path="/" serves the MCP
    # endpoint at the mount root, i.e. POST /mcp (the http_app default path
    # would double it to /mcp/mcp).
    mcp_asgi = mcp.http_app(path="/", transport="streamable-http", stateless_http=True)
    app = FastAPI(
        title="search-mcp",
        version=__version__,
        description="Web search API: search_web (Tavily) + fetch_url (SSRF-guarded).",
        lifespan=mcp_asgi.lifespan,
    )

    # x402 metering (env-gated, OFF by default) runs outermost.
    warn_if_payments_live()
    app.add_middleware(X402PaymentMiddleware)

    limiter = Limiter(key_func=get_remote_address)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_handler(request, exc):  # noqa: ANN001, ANN202
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "message": "Too many requests, slow down."},
        )

    @app.exception_handler(FetchError)
    async def _fetch_error_handler(request, exc):  # noqa: ANN001, ANN202
        return JSONResponse(status_code=422, content={"error": "fetch_failed", "message": str(exc)})

    @app.exception_handler(SearchError)
    async def _search_error_handler(request, exc):  # noqa: ANN001, ANN202
        return JSONResponse(
            status_code=502, content={"error": "search_failed", "message": str(exc)}
        )

    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    limit = rate_limit or DEFAULT_RATE_LIMIT

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            # Effective state (enabled/disabled) — never secret values.
            "x402": "enabled" if x402_effective_enabled() else "disabled",
            "stripe": "enabled" if stripe_effective_enabled() else "disabled",
        }

    app.include_router(billing_router)

    @app.post("/search")
    @limiter.limit(limit)
    async def search(request: Request, body: SearchRequest):  # noqa: ANN001, ANN202
        return {"results": _search.search_web(body.query, count=body.count)}

    @app.post("/fetch")
    @limiter.limit(limit)
    async def fetch(request: Request, body: FetchRequest):  # noqa: ANN001, ANN202
        return {"text": _fetch.fetch_url(body.url)}

    # MCP over streamable HTTP, mounted at /mcp (stateless: safe to mount).
    app.mount("/mcp", mcp_asgi)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "search_mcp.rest_app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
