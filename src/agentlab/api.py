"""Optional loopback HTTP API over a fixed startup corpus."""

import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .research.agent import ResearchAgent
from .research.corpus import Corpus
from .research.tasks import ResearchTaskStore, TaskCapacityExceeded, TaskConflict, TaskStoreError
from .research.task_events import emit_task_event


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


def create_app(
    corpus_root: Path, *, session_token: str | None = None, task_db: Path | None = None,
):
    try:
        from fastapi import FastAPI, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, ConfigDict, Field
        from starlette.middleware.trustedhost import TrustedHostMiddleware
    except ImportError as exc:
        raise RuntimeError("serve extra 未安装") from exc

    corpus = Corpus.load(corpus_root)
    task_store = ResearchTaskStore(task_db) if task_db is not None else None
    token = session_token or secrets.token_urlsafe(32)
    if not token:
        raise ValueError("session_token 必须是非空字符串")

    class ResearchRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        question: str = Field(min_length=1, max_length=4000)

    class ResearchTaskRequest(ResearchRequest):
        request_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

    app = FastAPI(
        title="Agent Lab",
        version="1.4.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(_LOOPBACK_HOSTS))

    @app.middleware("http")
    async def enforce_local_boundary(request: Request, call_next):
        if request.url.path == "/v1/research" or request.url.path.startswith("/v1/research/"):
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

    @app.exception_handler(TaskConflict)
    def task_conflict(_request: Request, _exc: TaskConflict) -> JSONResponse:
        emit_task_event("task_rejected", error_code="idempotency_conflict")
        return JSONResponse(status_code=409, content={"error": "idempotency_conflict"})

    @app.exception_handler(TaskStoreError)
    def task_store_error(_request: Request, _exc: TaskStoreError) -> JSONResponse:
        emit_task_event("task_rejected", error_code="task_store_unavailable")
        return JSONResponse(status_code=503, content={"error": "task_store_unavailable"})

    @app.exception_handler(TaskCapacityExceeded)
    def task_capacity_error(_request: Request, _exc: TaskCapacityExceeded) -> JSONResponse:
        emit_task_event("task_rejected", error_code="task_capacity_exceeded")
        return JSONResponse(
            status_code=429, content={"error": "task_capacity_exceeded"},
            headers={"Retry-After": "1"},
        )

    app.state.research_request_type = ResearchRequest
    app.state.validation_error_handler = validation_error
    app.state.session_token = token

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/research")
    def research(request: ResearchRequest) -> dict[str, Any]:
        return ResearchAgent(corpus).run(request.question).to_dict()

    if task_store is not None:
        @app.post("/v1/research/tasks")
        def submit_task(request: ResearchTaskRequest) -> JSONResponse:
            try:
                task, created = task_store.submit(request.request_key, request.question, corpus.content_hash)
            except ValueError:
                return JSONResponse(status_code=422, content={"error": "invalid_request"})
            emit_task_event(
                "task_submitted" if created else "task_replayed",
                task_id=task.id, attempt=task.attempt, status=task.status,
            )
            return JSONResponse(
                status_code=202 if created else 200, content=task.public_dict(),
                headers={"Location": f"/v1/research/tasks/{task.id}"},
            )

        @app.get("/v1/research/tasks/{task_id}")
        def get_task(task_id: str) -> JSONResponse:
            task = task_store.get(task_id)
            if task is None:
                return JSONResponse(status_code=404, content={"error": "task_not_found"})
            return JSONResponse(status_code=200, content=task.public_dict())

        @app.post("/v1/research/tasks/{task_id}/cancel")
        def cancel_task(task_id: str) -> JSONResponse:
            task = task_store.cancel(task_id)
            if task is None:
                return JSONResponse(status_code=404, content={"error": "task_not_found"})
            if task.error_code == "cancelled":
                emit_task_event("task_cancelled", task_id=task.id, attempt=task.attempt,
                                status=task.status, error_code="cancelled")
            return JSONResponse(status_code=200, content=task.public_dict())

        @app.get("/v1/research/task-metrics")
        def task_metrics() -> dict[str, Any]:
            return task_store.metrics()

    return app
