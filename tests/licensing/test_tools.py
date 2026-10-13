import json
import sys

from fastapi.testclient import TestClient

from fsm_license import admin, dev, openapi


def test_admin_cli_provisions_lists_and_reports_revision_conflict(service, monkeypatch, capsys):
    class ApiClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, target, **kwargs):
            return service.client.request(method, target, **kwargs)

    monkeypatch.setenv("FSM_LICENSE_INTERNAL_TOKEN", service.settings.internal_token)
    monkeypatch.setattr(admin.httpx, "Client", lambda **kwargs: ApiClient())
    base = ["--url", "http://127.0.0.1", "--allow-http"]
    put = [
        "put",
        "cli-order",
        "--customer",
        "synthetic-customer",
        "--expires-at",
        "2030-02-01T00:00:00Z",
        "--device-limit",
        "1",
        "--revision",
        "2",
    ]
    assert admin.main(base + put) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["activation_key"] and created["revision"] == 2
    assert admin.main(base + put) == 0
    assert json.loads(capsys.readouterr().out)["activation_key"] is None
    assert admin.main(base + ["devices", "cli-order"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    stale = put[:-1] + ["1"]
    assert admin.main(base + stale) == 1
    output = capsys.readouterr().out
    assert "409" in output and service.settings.internal_token not in output


def test_dev_launcher_migrates_and_generates_fresh_credentials(tmp_path, monkeypatch, capsys):
    instances = []
    monkeypatch.setattr(dev.uvicorn, "run", lambda app, **kwargs: instances.append((app, kwargs)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dev",
            "--sqlite",
            str(tmp_path / "nested" / "dev.sqlite3"),
            "--port",
            "8766",
        ],
    )
    dev.main()
    first_output = capsys.readouterr().out
    token = next(
        line.partition("=")[2]
        for line in first_output.splitlines()
        if line.startswith("FSM_LICENSE_INTERNAL_TOKEN=")
    )
    assert len(token) >= 32
    assert instances[0][1] == {"host": "127.0.0.1", "port": 8766}
    with TestClient(instances[0][0]) as client:
        assert client.get("/healthz").status_code == 200
        result = client.put(
            "/internal/v1/entitlements/dev-order",
            headers={
                "Authorization": f"Bearer {token}",
            },
            json={
                "external_customer_id": "synthetic-customer",
                "expires_at": "2030-12-31T00:00:00Z",
                "device_limit": 1,
                "status": "active",
                "revision": 1,
            },
        )
        assert result.status_code == 200 and result.json()["activation_key"]
    instances[0][0].state.database.dispose()
    dev.main()
    second_output = capsys.readouterr().out
    assert token not in second_output
    instances[1][0].state.database.dispose()


def test_openapi_export_documents_all_auth_requirements(tmp_path, monkeypatch):
    output = tmp_path / "nested" / "openapi.json"
    monkeypatch.setattr(sys, "argv", ["openapi", str(output)])
    openapi.main()
    schema = json.loads(output.read_text(encoding="utf-8"))
    assert schema["paths"]["/internal/v1/entitlements/{external_id}"]["put"]["security"] == [
        {"ServiceBearer": []},
    ]
    assert schema["paths"]["/v1/activations"]["post"]["security"] == [
        {"DeviceTimestamp": [], "DeviceNonce": [], "DeviceSignature": []},
    ]
    assert "X-Device-Signature" in output.read_text(encoding="utf-8")
