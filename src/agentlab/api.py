"""Optional loopback HTTP API over a fixed startup corpus."""

import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .research.agent import ResearchAgent
from .research.corpus import Corpus


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


def create_app(corpus_root: Path, *, session_token: str | None = None):
    try:
        from fastapi import FastAPI, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, ConfigDict, Field
        from starlette.middleware.trustedhost import TrustedHostMiddleware
    except ImportError as exc:
        raise RuntimeError("serve extra 未安装") from exc

    corpus = Corpus.load(corpus_root)
    token = session_token or secrets.token_urlsafe(32)
    if not token:
        raise ValueError("session_token 必须是非空字符串")

    class ResearchRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        question: str = Field(min_length=1, max_length=4000)

    app = FastAPI(title="Agent Lab", version="1.3.0", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(_LOOPBACK_HOSTS))

    @app.middleware("http")
    async def enforce_local_boundary(request: Request, call_next):
        if request.url.path == "/v1/research":
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                    return JSONResponse(status_code=403, content={"error": "forbidden"})
            supplied = request.headers.get("x-agentlab-token", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse(status_code=403, content={"error": "forbidden"})
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    def validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_request"})

    app.state.research_request_type = ResearchRequest
    app.state.validation_error_handler = validation_error
    app.state.session_token = token

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/research")
    def research(request: ResearchRequest) -> dict[str, Any]:
        return ResearchAgent(corpus).run(request.question).to_dict()

    return app
