"""M027 — local/private web dashboard unit tests (no Git, loopback only)."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from trajectory_os.web import model, projection, render, server


def _seed_goal(root: Path) -> str:
    from trajectory_os.goals import launch
    from trajectory_os.graph import store as graph_store

    goal_id = "g-web-unit"
    spec = {
        "schema_version": 1,
        "goal_id": goal_id,
        "objective": "Prove the web projection surface.",
        "nodes": [{
            "node_id": "n-web",
            "title": "Web mission",
            "priority": 50,
            "depends_on": [],
            "acceptance_criteria": [{
                "criterion_id": "ac-web",
                "statement": "web is a projection"}],
            "mission_ref": {"mission_id": "m-web-unit", "required": True},
            "resources": {"cpu_slots": 1},
            "budgets": {"repair_budget": 0, "max_attempts": 1},
        }],
    }
    defaults = launch.MissionLaunchDefaults(
        repo_root=None, baseline_revision="base", repair_budget=0)
    launch.provision_from_spec(str(root), spec, defaults)
    graph_store.create_graph(str(root), spec, repo_root=defaults.repo_root,
                             baseline_revision=defaults.baseline_revision)
    return goal_id


class _Client:
    def __init__(self, port: int, token: str | None = None) -> None:
        self.port = port
        self.token = token

    def request(self, method: str, path: str,
                body: object | None = None,
                headers: dict[str, str] | None = None,
                ) -> tuple[int, dict[str, str], str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port,
                                                timeout=10)
        request_headers = dict(headers or {})
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload,
                           headers=request_headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8")
        connection.close()
        return response.status, {
            key.lower(): value for key, value in response.getheaders()
        }, text


@pytest.fixture()
def web(tmp_path: Path) -> Iterator[tuple[_Client, Path]]:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_goal(root)
    config = model.WebConfig(root=str(root), default_goal_id=goal_id,
                             port=0).validate()
    httpd = server.create_server(server.WebApp(config))
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
        daemon=True)
    thread.start()
    try:
        yield _Client(httpd.server_address[1]), root
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_config_rejects_non_loopback_host() -> None:
    with pytest.raises(model.WebError) as exc:
        model.WebConfig(root="/tmp/x", host="0.0.0.0").validate()
    assert exc.value.code == model.E_INVALID_CONFIG


def test_read_routes(web: tuple[_Client, Path]) -> None:
    client, _root = web
    status, headers, body = client.request("GET", "/api/overview")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    document = json.loads(body)
    assert document["count"] == 1
    status, _headers, body = client.request("GET", "/")
    assert status == 200 and "TrajectoryOS" in body
    status, _headers, body = client.request("GET", "/api/controls")
    assert status == 200
    assert set(json.loads(body)["controls"]) == model.CONTROLS
    assert client.request("GET", "/healthz")[0] == 200
    assert client.request("GET", "/api/telemetry")[0] == 200
    assert client.request("GET", "/missing")[0] == 404


def test_goal_routes(web: tuple[_Client, Path]) -> None:
    client, _root = web
    status, _headers, body = client.request("GET", "/api/goals/g-web-unit")
    assert status == 200
    assert json.loads(body)["snapshot"]["goal"]["goal_id"] == "g-web-unit"
    status, _headers, body = client.request(
        "GET", "/api/goals/g-web-unit/events")
    assert status == 200 and json.loads(body)["count"] >= 1
    status, _headers, body = client.request("GET", "/goal/g-web-unit")
    assert status == 200 and "Events" in body


def test_forbidden_non_loopback_host(web: tuple[_Client, Path]) -> None:
    client, _root = web
    status, _headers, body = client.request(
        "GET", "/api/overview", headers={"Host": "evil.example:1"})
    assert status == 403
    assert json.loads(body)["error"] == model.E_FORBIDDEN


def test_controls_stop_and_rejections(web: tuple[_Client, Path]) -> None:
    client, root = web
    status, _headers, body = client.request(
        "POST", "/api/control/stop",
        body={"goal_id": "g-web-unit", "reason": "unit"})
    assert status == 200
    assert json.loads(body)["control"] == "stop"
    assert (root / "goals" / "g-web-unit" / "stop.json").is_file()

    status, _headers, body = client.request(
        "POST", "/api/control/clear-stop", body={"goal_id": "g-web-unit"})
    assert status == 200
    assert not (root / "goals" / "g-web-unit" / "stop.json").exists()

    status, _headers, body = client.request(
        "POST", "/api/control/refresh-events", body={"goal_id": "g-web-unit"})
    assert status == 200
    assert json.loads(body)["result"]["count"] >= 1

    status, _headers, _body = client.request(
        "POST", "/api/control/nuke", body={})
    assert status == 404

    connection = http.client.HTTPConnection(
        "127.0.0.1", client.port, timeout=10)
    connection.request("POST", "/api/control/stop", body=b"not-json",
                       headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    assert response.status == 400
    response.read()
    connection.close()


def test_auth_token_required_when_configured(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_goal(root)
    config = model.WebConfig(root=str(root), default_goal_id=goal_id, port=0,
                             token="s3cret").validate()
    httpd = server.create_server(server.WebApp(config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        anonymous = _Client(httpd.server_address[1])
        assert anonymous.request("GET", "/api/overview")[0] == 401
        authorized = _Client(httpd.server_address[1], token="s3cret")
        assert authorized.request("GET", "/api/overview")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_render_escapes_dynamic_text() -> None:
    document = {
        "goal_id": "<script>",
        "count": 1,
        "goals": [{
            "goal_id": "<script>alert(1)</script>", "state": "COMPLETE",
            "reason": "OK", "complete": True, "criteria_proven": 1,
            "criteria_total": 1, "blockers": 0, "gate": "DONE",
            "stop_requested": False, "status": "OK",
        }],
    }
    html = render.render_index("/root", document)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_projections_are_read_only(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    goal_id = _seed_goal(root)
    before = sorted(p.name for p in root.rglob("*"))
    projection.overview(str(root))
    projection.dashboard(str(root), goal_id)
    after = sorted(p.name for p in root.rglob("*"))
    assert before == after


def test_no_git_trust_boundary_write_in_web() -> None:
    verbs = ("commit", "push", "merge", "reset", "restore", "clean", "stash",
             "rebase", "switch", "checkout", "pull", "fetch", "cherry-pick")
    source = Path(server.__file__).read_text(encoding="utf-8")
    source += Path(model.__file__).read_text(encoding="utf-8")
    offenders = [verb for verb in verbs
                 if f'"git", "{verb}"' in source or f"'git', '{verb}'" in source]
    assert offenders == []
