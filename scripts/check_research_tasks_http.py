"""Opt-in loopback smoke test: real CLI server, worker, HTTP and restart."""

from __future__ import annotations

import json
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def cli(*args: str) -> list[str]:
    return [sys.executable, "-c", "from agentlab.cli import main; raise SystemExit(main())", *args]


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run() -> dict[str, object]:
    # No Provider and no non-loopback requests. State and session tokens stay local.
    with tempfile.TemporaryDirectory(prefix="agentlab-task-http-") as directory:
        root = Path(directory)
        corpus = root / "corpus"
        corpus.mkdir()
        (corpus / "fact.md").write_text("The Helix fixture status is ready.", encoding="utf-8")
        database = root / "state" / "tasks.sqlite3"
        payload = {"request_key": "http-smoke-1", "question": "What is the Helix fixture status?"}
        api_events = []

        def start():
            with socket.socket() as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                port = port_socket.getsockname()[1]
            process = subprocess.Popen(
                cli("serve", "--corpus", str(corpus), "--task-db", str(database), "--port", str(port)),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            tokens = queue.Queue()

            def drain():
                # Drain continuously: capacity tests emit more than a pipe buffer.
                for line in process.stderr:
                    if line.startswith("session token: "):
                        tokens.put(line.strip().removeprefix("session token: "))
                    elif line.startswith("{"):
                        api_events.append(json.loads(line))

            threading.Thread(target=drain, daemon=True).start()
            try:
                token = tokens.get(timeout=10)
                base = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with urllib.request.urlopen(base + "/health", timeout=1) as response:
                            assert response.status == 200
                        return process, base, token
                    except urllib.error.URLError:
                        assert time.monotonic() < deadline and process.poll() is None, "server unavailable"
                        time.sleep(0.05)
            except BaseException:
                stop(process)
                raise

        def request(base, token, path, body=None):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json", "X-AgentLab-Token": token},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.load(error)

        server, base, token = start()
        try:
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: request(base, token, "/v1/research/tasks", payload), range(4)))
            assert sorted(code for code, _ in results) == [200, 200, 200, 202]
            assert len({body["id"] for _, body in results}) == 1
            location = "/v1/research/tasks/" + results[0][1]["id"]
            assert request(base, "invalid", location)[0] == 403
            assert request(base, token, "/v1/research/tasks", dict(payload, question="changed"))[0] == 409
            assert request(base, token, location)[1]["status"] == "pending"
            assert request(base, "invalid", "/v1/research/task-metrics")[0] == 403
            assert request(base, token, "/v1/research/task-metrics")[1]["counts"]["pending"] == 1
        finally:
            stop(server)

        # The API is stopped while an independent CLI worker processes the committed task.
        worker = subprocess.run(
            cli("research-worker", "--corpus", str(corpus), "--task-db", str(database), "--once"),
            capture_output=True, text=True, timeout=15,
        )
        assert worker.returncode == 0, "worker failed"
        assert json.loads(worker.stdout)["status"] == "succeeded"
        worker_events = [json.loads(line) for line in worker.stderr.splitlines()]
        assert [event["event"] for event in worker_events] == ["worker_claimed", "worker_finished"]
        assert all(event["task_id"] == results[0][1]["id"] for event in worker_events)
        assert worker_events[-1]["duration_ms"] >= 0
        server, base, new_token = start()
        try:
            assert request(base, token, location)[0] == 403
            code, final = request(base, new_token, location)
            assert code == 200 and final["status"] == "succeeded" and final["attempt"] == 1
            assert final["result"]["answer"]["status"] == "answered"
            assert request(base, new_token, "/v1/research/tasks", payload) == (200, final)
            # Cancellation must not replace an already committed success.
            assert request(base, new_token, location + "/cancel", {}) == (200, final)
            for i in range(100):
                code, queued = request(base, new_token, "/v1/research/tasks", {
                    "request_key": f"capacity-{i}", "question": "Synthetic queued question",
                })
                assert code == 202
            overflow = {"request_key": "capacity-overflow", "question": "Synthetic queued question"}
            assert request(base, new_token, "/v1/research/tasks", overflow)[0] == 429
            assert request(base, new_token, "/v1/research/tasks", payload) == (200, final)
            cancel_location = "/v1/research/tasks/" + queued["id"] + "/cancel"
            assert request(base, "invalid", cancel_location, {})[0] == 403
            code, cancelled = request(base, new_token, cancel_location, {})
            assert code == 200 and cancelled["error_code"] == "cancelled"
            assert request(base, new_token, cancel_location, {}) == (200, cancelled)
            assert request(base, new_token, "/v1/research/tasks", overflow)[0] == 202
            metrics = request(base, new_token, "/v1/research/task-metrics")[1]
            assert metrics["counts"] == {"pending": 100, "running": 0, "succeeded": 1, "failed": 1}
            assert metrics["errors"] == {"cancelled": 1}
        finally:
            stop(server)
        events_text = json.dumps(api_events + worker_events)
        assert all(private not in events_text for private in (payload["question"], payload["request_key"],
                                                              str(root), token, new_token))
    return {
        "passed": True, "transport": "real loopback HTTP", "concurrent_submissions": 4,
        "created_tasks": 1, "independent_worker": True, "api_restart_preserves_result": True,
        "old_session_rejected": True, "provider_calls": 0,
        "capacity_rejected_with_429": True, "cancel_releases_capacity": True,
        "protected_metrics": True, "redacted_lifecycle_events": True,
    }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
