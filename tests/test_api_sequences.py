from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vkit.operations import api_sequences
from vkit.operations.api_sequences import SCHEMATHESIS_VERSION, check_api_sequences


class _Service(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, bug: bool = False):
        self.bug = bug
        self.oversize_response = False
        self.redirect_generated = False
        self.redirect_url = ""
        self.items: dict[str, dict] = {}
        self.reset_count = 0
        self.redirect_target_count = 0
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _Service

    def log_message(self, *_args):
        return

    def _reply(
        self, status: int, value: dict | None = None, headers: dict | None = None
    ):
        raw = b"" if value is None else json.dumps(value).encode("utf-8")
        self.send_response(status)
        if value is not None:
            self.send_header("Content-Type", "application/json")
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_POST(self):
        if self.path == "/__reset":
            self.server.items.clear()
            self.server.reset_count += 1
            self._reply(204)
            return
        if self.path == "/__redirect":
            self._reply(307, headers={"Location": "/__redirect_target"})
            return
        if self.path == "/__redirect_target":
            self.server.redirect_target_count += 1
            self.server.items.clear()
            self._reply(204)
            return
        if self.path == "/items" and self.server.redirect_generated:
            self._reply(307, headers={"Location": self.server.redirect_url})
            return
        length = min(int(self.headers.get("Content-Length", "0")), 1_048_577)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            body = {"name": "generated"}
        self.server.items["one"] = (
            body if isinstance(body, dict) else {"name": "generated"}
        )
        self._reply(201, {"id": "one", **self.server.items["one"]})

    def do_DELETE(self):
        key = self.path.rsplit("/", 1)[-1]
        self.server.items.pop(key, None)
        self._reply(204)

    def do_GET(self):
        key = self.path.rsplit("/", 1)[-1]
        if key in self.server.items:
            self._reply(200, {"id": key, **self.server.items[key]})
        elif self.server.bug:
            name = "x" * 1_048_576 if self.server.oversize_response else "deleted"
            self._reply(200, {"id": key, "name": name})
        else:
            self._reply(404, {"detail": "missing"})


@pytest.fixture
def api_service(monkeypatch):
    server = _Service()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("VKIT_API_SEQUENCE_BASE_URL", base)
    monkeypatch.setenv("VKIT_API_SEQUENCE_RESET_URL", f"{base}/__reset")
    try:
        yield server, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _schema(root) -> None:
    item_schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
        "required": ["id", "name"],
    }
    document = {
        "openapi": "3.0.3",
        "info": {"title": "Items", "version": "1"},
        "paths": {
            "/items": {
                "post": {
                    "operationId": "createItem",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "required": ["name"],
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {"application/json": {"schema": item_schema}},
                            "links": {
                                "deleteCreated": {
                                    "operationId": "deleteItem",
                                    "parameters": {"itemId": "$response.body#/id"},
                                }
                            },
                        }
                    },
                }
            },
            "/items/{itemId}": {
                "parameters": [
                    {
                        "name": "itemId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "delete": {
                    "operationId": "deleteItem",
                    "responses": {
                        "204": {
                            "description": "deleted",
                            "links": {
                                "readDeleted": {
                                    "operationId": "getItem",
                                    "parameters": {"itemId": "$request.path.itemId"},
                                }
                            },
                        }
                    },
                },
                "get": {
                    "operationId": "getItem",
                    "responses": {
                        "200": {
                            "description": "read",
                            "content": {"application/json": {"schema": item_schema}},
                        },
                        "404": {"description": "missing"},
                    },
                },
            },
        },
    }
    path = root / "openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")


def test_local_stateful_campaign_passes_when_deleted_items_return_404(
    tmp_path, api_service
):
    server, _base = api_service
    _schema(tmp_path)

    result = check_api_sequences(tmp_path, "openapi.json", max_examples=12, max_steps=6)

    assert result["status"] == "NO_VIOLATION", (
        result.get("status"),
        result.get("detail"),
        result.get("campaign"),
    )
    assert result["engine"] == {"name": "Schemathesis", "version": SCHEMATHESIS_VERSION}
    assert result["campaign"]["requested_examples"] == 12
    assert result["campaign"]["state_machine_setups"] >= 12
    assert result["campaign"]["steps"] > 0
    assert server.reset_count == result["campaign"]["state_machine_setups"]
    assert str(result["campaign"]["state_machine_setups"]) in result["detail"]
    assert "not proof of universal" in result["scope"]


def test_minimized_use_after_free_failure_must_replay_after_fresh_reset(
    tmp_path, api_service
):
    server, _base = api_service
    server.bug = True
    _schema(tmp_path)

    result = check_api_sequences(tmp_path, "openapi.json", max_examples=15, max_steps=6)

    assert result["status"] == "BUG_REPRODUCED", (
        result.get("status"),
        result.get("detail"),
        result.get("campaign"),
    )
    assert result["counterexample"]["failure_ids"] == [
        "schemathesis.openapi.checks.UseAfterFree"
    ]
    assert (
        result["counterexample"]["replayed_failure_ids"]
        == result["counterexample"]["failure_ids"]
    )
    steps = result["counterexample"]["steps"]
    assert steps[0]["case"]["method"] == "POST"
    assert [
        (step["case"]["method"], step["case"]["operation_id"]) for step in steps[-2:]
    ] == [("DELETE", "deleteItem"), ("GET", "getItem")]
    assert [step["status_code"] for step in steps[-2:]] == [204, 200]
    get_response = steps[-1]["response"]
    body = base64.b64decode(get_response["body_base64"])
    assert body == json.dumps({"id": "one", "name": "deleted"}).encode()
    assert get_response["body_bytes"] == len(body)
    assert get_response["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert get_response["headers"]["content-type"] == ["application/json"]
    assert result["campaign"]["replay_resets"] == 1
    assert result["replay_scope"] == (
        "same engine failure identifiers only; response equality is not asserted"
    )
    assert server.reset_count == (
        result["campaign"]["resets_before_replay"] + result["campaign"]["replay_resets"]
    )
    assert len(result["counterexample"]["sha256"]) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("servers", [{"url": "http://203.0.113.10"}]),
        ("webhooks", {}),
        ("security", [{"token": []}]),
    ],
)
def test_schema_cannot_select_api_hosts_or_authentication(
    tmp_path, api_service, field, value
):
    server, _base = api_service
    _schema(tmp_path)
    path = tmp_path / "openapi.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document[field] = value
    path.write_text(json.dumps(document), encoding="utf-8")

    result = check_api_sequences(tmp_path, "openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert server.reset_count == 0


def test_schema_cannot_add_arbitrary_request_headers(tmp_path, api_service):
    server, _base = api_service
    _schema(tmp_path)
    path = tmp_path / "openapi.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["paths"]["/items"]["post"]["parameters"] = [
        {
            "name": "X-Api-Key",
            "in": "header",
            "required": True,
            "schema": {"type": "string"},
        }
    ]
    path.write_text(json.dumps(document), encoding="utf-8")

    result = check_api_sequences(tmp_path, "openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert server.reset_count == 0


@pytest.mark.parametrize("reference", ["operation", "schema"])
def test_schema_cannot_follow_external_operation_or_schema_references(
    tmp_path, api_service, reference
):
    server, _base = api_service
    _schema(tmp_path)
    path = tmp_path / "openapi.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if reference == "operation":
        link = document["paths"]["/items"]["post"]["responses"]["201"]["links"][
            "deleteCreated"
        ]
        link.clear()
        link["operationRef"] = "https://api.example/openapi.json#/paths/~1items/post"
    else:
        document["components"] = {
            "schemas": {"Remote": {"$ref": "https://api.example/schema.json"}}
        }
    path.write_text(json.dumps(document), encoding="utf-8")

    result = check_api_sequences(tmp_path, "openapi.json")

    assert result["status"] == "UNSUPPORTED"
    assert server.reset_count == 0


def test_oversized_response_is_inconclusive_not_a_reproduced_bug(tmp_path, api_service):
    server, _base = api_service
    server.bug = True
    server.oversize_response = True
    _schema(tmp_path)

    result = check_api_sequences(tmp_path, "openapi.json", max_examples=6, max_steps=4)

    assert result["status"] == "INCONCLUSIVE"
    assert result["detail"] == "API response body exceeds 1 MiB"


def test_reset_redirect_is_not_followed_and_campaign_is_inconclusive(
    tmp_path, api_service, monkeypatch
):
    server, base = api_service
    _schema(tmp_path)
    monkeypatch.setenv("VKIT_API_SEQUENCE_RESET_URL", f"{base}/__redirect")

    result = check_api_sequences(tmp_path, "openapi.json", max_examples=4, max_steps=3)

    assert result["status"] == "INCONCLUSIVE"
    assert server.redirect_target_count == 0


def test_generated_api_redirect_is_not_followed_to_another_origin(
    tmp_path, api_service
):
    server, _base = api_service
    server.redirect_generated = True
    server.redirect_url = f"http://127.0.0.2:{server.server_port}/__reset"
    _schema(tmp_path)

    result = check_api_sequences(tmp_path, "openapi.json", max_examples=4, max_steps=3)

    assert result["status"] == "BUG_REPRODUCED"
    assert result["counterexample"]["steps"][0]["status_code"] == 307
    assert server.redirect_target_count == 0


def test_local_reference_closure_is_captured_and_used_in_engine_worker(
    tmp_path, api_service
):
    server, _base = api_service
    _schema(tmp_path)
    schema = tmp_path / "openapi.json"
    document = json.loads(schema.read_text(encoding="utf-8"))
    document["paths"]["/items/{itemId}"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] = {"$ref": "./components.json#/components/schemas/Item"}
    schema.write_text(json.dumps(document), encoding="utf-8")
    (tmp_path / "components.json").write_text(
        json.dumps(
            {
                "components": {
                    "schemas": {
                        "Item": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "name": {"type": "string"},
                            },
                            "required": ["id", "name"],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = check_api_sequences(tmp_path, "openapi.json", max_examples=4, max_steps=4)

    assert result["status"] == "NO_VIOLATION", result
    assert [source["path"] for source in result["schema_files"]] == [
        "components.json",
        "openapi.json",
    ]
    assert server.reset_count == result["campaign"]["state_machine_setups"]


def test_worker_output_overflow_cancels_or_rejects_the_worker_result(
    tmp_path, api_service, monkeypatch
):
    _schema(tmp_path)
    monkeypatch.setattr(api_sequences, "MAX_WORKER_OUTPUT_BYTES", 1)

    result = check_api_sequences(tmp_path, "openapi.json", max_examples=4, max_steps=3)

    assert result["status"] == "INCONCLUSIVE"
    assert result["detail"] == (
        "API sequence worker output exceeds the 256 KiB capture limit"
    )


def test_api_sequence_cli_mcp_and_mutating_annotation(make_project, api_service):
    from helpers import McpClient, vkit

    server, _ = api_service
    server.bug = True
    project = make_project({}, [], accept=False)
    _schema(project)
    code, body = vkit(
        project,
        "compute",
        "check-api-sequences",
        "--schema-path",
        "openapi.json",
        "--max-examples",
        "15",
        "--max-steps",
        "6",
    )
    assert (code, body["status"]) == (1, "BUG_REPRODUCED"), body
    client = McpClient(project)
    try:
        tools = client.call("help", name="check_api_sequences")[0]["tools"]
        operation = next(
            tool for tool in tools if tool["name"] == "check_api_sequences"
        )
        assert operation["annotations"]["readOnlyHint"] is False
        body, error = client.call(
            "check_api_sequences",
            schema_path="openapi.json",
            max_examples=15,
            max_steps=6,
        )
        assert not error and body["status"] == "BUG_REPRODUCED", body
        assert body["counterexample"]["failure_ids"] == [
            "schemathesis.openapi.checks.UseAfterFree"
        ]
    finally:
        client.close()
