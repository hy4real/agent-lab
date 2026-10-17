"""Opt-in check of an explicitly selected, already-started local Compose project.

Stops its services on exit; retains all containers and volumes. No Provider calls.
Raw API logs, which contain a session credential, are never printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def run(project: str) -> dict[str, object]:
    root = Path(__file__).resolve().parents[1]

    def compose(*args):
        result = subprocess.run(["docker", "compose", "-p", project, *args], cwd=root,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError("Compose operation failed (raw logs withheld)")
        return result.stdout

    address = compose("port", "api", "8000").strip()
    assert address.startswith("127.0.0.1:"), "API must publish only to loopback"
    base = "http://" + address

    def token():
        lines = compose("logs", "--no-log-prefix", "--no-color", "api").splitlines()
        return [line.removeprefix("session token: ") for line in lines
                if line.startswith("session token: ")][-1]

    def request(credential, path, body=None):
        req = urllib.request.Request(base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              "X-AgentLab-Token": credential})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def completed(credential, task_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            code, body = request(credential, "/v1/research/tasks/" + task_id)
            assert code == 200
            if body["status"] in {"succeeded", "failed"}:
                assert body["status"] == "succeeded", "fixture task failed"
                return body
            time.sleep(0.1)
        raise AssertionError("worker did not complete fixture task")

    try:
        credential = token()
        assert request("invalid", "/v1/research/task-metrics")[0] == 403
        for service in ("api", "worker"):
            metadata = json.loads(compose("exec", "-T", service, "python", "-c",
                "import json,os,stat; print(json.dumps([os.getuid(), "
                "stat.S_IMODE(os.stat('/state').st_mode), "
                "stat.S_IMODE(os.stat('/state/research.sqlite3').st_mode)]))"))
            assert metadata == [10001, 0o700, 0o600]
        key = "docker-smoke-" + uuid.uuid4().hex
        payload = {"request_key": key, "question": "What does Agent Lab provide?"}
        code, first = request(credential, "/v1/research/tasks", payload)
        assert code == 202
        final = completed(credential, first["id"])
        assert request(credential, "/v1/research/tasks", payload) == (200, final)

        compose("stop", "worker")
        code, second = request(credential, "/v1/research/tasks", dict(payload, request_key=key + "-queued"))
        assert code == 202 and second["status"] == "pending"
        assert request(credential, "/v1/research/task-metrics")[1]["counts"]["pending"] >= 1
        compose("start", "worker")
        completed(credential, second["id"])

        compose("restart", "api")
        deadline = time.monotonic() + 30
        while True:
            try:
                new_credential = token()
                assert new_credential != credential
                assert request(new_credential, "/health")[0] == 200
                break
            except (AssertionError, IndexError, urllib.error.URLError, ConnectionError):
                assert time.monotonic() < deadline, "API restart timed out"
                time.sleep(0.2)
        assert request(credential, "/v1/research/tasks/" + first["id"])[0] == 403
        assert request(new_credential, "/v1/research/tasks/" + first["id"]) == (200, final)
        events = [json.loads(line) for line in compose("logs", "--no-log-prefix", "--no-color", "worker").splitlines()
                  if line.startswith("{") and '"event"' in line]
        assert any(event.get("task_id") == first["id"] and event["event"] == "worker_finished"
                   and event["status"] == "succeeded" for event in events)
        event_text = json.dumps(events)
        assert all(value not in event_text for value in (key, payload["question"], credential, new_credential))
        return {"passed": True, "nonroot_uid": 10001, "private_volume": True,
                "worker_stop_backlog_recovery": True, "api_restart_preserves_result": True,
                "old_session_rejected": True, "worker_events": True, "provider_calls": 0}
    finally:
        compose("stop")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Explicit Compose test project; stopped after check")
    print(json.dumps(run(parser.parse_args().project), sort_keys=True))
