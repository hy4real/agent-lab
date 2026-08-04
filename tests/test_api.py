"""Optional loopback API contract tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from agentlab.api import create_app


def _app(tmp_path: Path):
    (tmp_path / "fact.md").write_text("The API fixture state is healthy.", encoding="utf-8")
    return create_app(tmp_path)


def _endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_health_and_research(tmp_path: Path) -> None:
    app = _app(tmp_path)
    research_route = next(route for route in app.routes if getattr(route, "path", None) == "/v1/research")
    assert research_route.body_field is not None
    assert _endpoint(app, "/health")() == {"status": "ok"}
    request = app.state.research_request_type(question="What is the API fixture state?")
    assert _endpoint(app, "/v1/research")(request)["answer"]["status"] == "answered"


def test_request_cannot_submit_local_path(tmp_path: Path) -> None:
    app = _app(tmp_path)
    try:
        app.state.research_request_type.model_validate(
            {"question": "What is the state?", "corpus": "/private/secret"}
        )
    except ValidationError as exc:
        response = app.state.validation_error_handler(None, RequestValidationError(exc.errors()))
    else:
        raise AssertionError("path field should be rejected")
    assert response.status_code == 422
    assert json.loads(response.body) == {"error": "invalid_request"}
    assert b"/private/secret" not in response.body


@pytest.mark.anyio
async def test_research_http_requires_loopback_host_and_session_token(tmp_path: Path) -> None:
    app = _app(tmp_path)
    payload = {"question": "What is the API fixture state?"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.post("/v1/research", json=payload)).status_code == 403
        response = await client.post(
            "/v1/research",
            json=payload,
            headers={"X-AgentLab-Token": app.state.session_token},
        )
        assert response.status_code == 200
        assert response.json()["answer"]["status"] == "answered"
        assert (await client.post(
            "/v1/research",
            json=payload,
            headers={"Host": "attacker.example", "X-AgentLab-Token": app.state.session_token},
        )).status_code == 400


@pytest.mark.anyio
async def test_research_http_rejects_non_loopback_origin(tmp_path: Path) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.post(
            "/v1/research",
            json={"question": "What is the API fixture state?"},
            headers={
                "Origin": "http://attacker.example",
                "X-AgentLab-Token": app.state.session_token,
            },
        )
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
