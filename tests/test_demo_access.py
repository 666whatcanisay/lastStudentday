"""Security acceptance for the temporary public-demo boundary."""

import time
import subprocess
import sys
from collections import deque

import pytest
from fastapi.testclient import TestClient

from app import main
from app.services import local_security
from app.services.demo_auth import (
    COOKIE_NAME, DEMO_AUTH, DemoSettings, password_hash,
    validated_public_origin, verify_password,
)


PUBLIC_ORIGIN = "https://ocr-demo.example.ts.net"


@pytest.fixture
def demo(monkeypatch):
    settings = DemoSettings(
        enabled=True,
        public_origin=PUBLIC_ORIGIN,
        guest_hash=password_hash("guest-demo-password-123"),
        admin_hash=password_hash("admin-demo-password-456"),
    )
    monkeypatch.setattr(DEMO_AUTH, "settings", settings)
    monkeypatch.setattr(local_security, "ALLOWED_HOSTS",
                        {"127.0.0.1", "localhost", "::1", "ocr-demo.example.ts.net"})
    monkeypatch.setattr(local_security, "LOCAL_CORS_ORIGINS",
                        local_security.LOCAL_CORS_ORIGINS + [PUBLIC_ORIGIN])
    DEMO_AUTH._sessions.clear()
    DEMO_AUTH._failures.clear()
    DEMO_AUTH._active.clear()
    DEMO_AUTH._global_active.clear()
    DEMO_AUTH._qa_calls.clear()
    yield TestClient(main.app, base_url=PUBLIC_ORIGIN)
    DEMO_AUTH._sessions.clear()


@pytest.fixture
def single_demo(monkeypatch):
    settings = DemoSettings(
        enabled=True,
        public_origin=PUBLIC_ORIGIN,
        guest_hash="",
        admin_hash=password_hash("single-demo-password-123"),
    )
    monkeypatch.setattr(DEMO_AUTH, "settings", settings)
    monkeypatch.setattr(local_security, "ALLOWED_HOSTS",
                        {"127.0.0.1", "localhost", "::1", "ocr-demo.example.ts.net"})
    monkeypatch.setattr(local_security, "LOCAL_CORS_ORIGINS",
                        local_security.LOCAL_CORS_ORIGINS + [PUBLIC_ORIGIN])
    DEMO_AUTH._sessions.clear()
    DEMO_AUTH._failures.clear()
    DEMO_AUTH._active.clear()
    DEMO_AUTH._global_active.clear()
    DEMO_AUTH._qa_calls.clear()
    yield TestClient(main.app, base_url="http://127.0.0.1")
    DEMO_AUTH._sessions.clear()


def _login(client, password):
    return client.post("/api/auth/login", json={"password": password},
                       headers={"Origin": PUBLIC_ORIGIN})


def test_password_hash_and_exact_origin():
    encoded = password_hash("strong-demo-password")
    assert "strong-demo-password" not in encoded
    assert verify_password("strong-demo-password", encoded)
    assert not verify_password("wrong-password", encoded)
    with pytest.raises(ValueError):
        password_hash("short")
    assert verify_password("twelve chars", password_hash("twelve chars", min_length=12))
    assert validated_public_origin(PUBLIC_ORIGIN + "/") == PUBLIC_ORIGIN
    for bad in ("http://ocr-demo.example.ts.net", "https://*.ts.net",
                "https://evil.example.com", PUBLIC_ORIGIN + "/api"):
        with pytest.raises(ValueError):
            validated_public_origin(bad)


def test_unauthenticated_data_and_bad_host_are_rejected(demo):
    for path in ("/api/document/list", "/api/file/preview/1",
                 "/api/document/export/report?doc_id=1", "/openapi.json"):
        assert demo.get(path).status_code == 401
    assert demo.get("/api/document/list", headers={"Host": "evil.example"}).status_code == 403
    assert demo.get("/api/auth/me").json()["data"]["authenticated"] is False


def test_guest_business_access_but_no_maintenance(demo):
    response = _login(demo, "guest-demo-password-123")
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "guest"
    assert response.cookies.get(COOKIE_NAME)
    assert "Secure" in response.headers["set-cookie"]
    assert demo.get("/api/document/field-specs?doc_type=invoice").status_code == 200
    assert demo.get("/api/document/list?in_recycle_bin=true").status_code == 403
    assert demo.get("/api/maintenance/restore-info").status_code == 403
    assert demo.post("/api/maintenance/backup", headers={"Origin": PUBLIC_ORIGIN}).status_code == 403
    assert demo.post("/api/rag/index", json={}, headers={"Origin": PUBLIC_ORIGIN}).status_code == 403
    assert demo.delete("/api/document/999", headers={"Origin": PUBLIC_ORIGIN}).status_code == 403
    assert demo.post("/api/document/999/restore", headers={"Origin": PUBLIC_ORIGIN}).status_code == 403
    assert demo.post("/api/rag/rewrite", json={"query": "x"},
                     headers={"Origin": "https://evil.example"}).status_code == 403


def test_admin_can_open_maintenance_and_logout_revokes(demo):
    assert _login(demo, "admin-demo-password-456").json()["data"]["role"] == "admin"
    assert demo.get("/api/maintenance/restore-info").status_code == 200
    assert demo.get("/openapi.json").status_code == 200
    assert demo.post("/api/auth/logout", headers={"Origin": PUBLIC_ORIGIN}).status_code == 200
    assert demo.get("/api/maintenance/restore-info").status_code == 401


def test_single_password_is_admin_and_local_cookie_round_trips(single_demo):
    assert DEMO_AUTH.settings.single_password
    for path in ("/api/document/list", "/api/file/preview/1",
                 "/api/document/export/report?doc_id=1", "/api/maintenance/restore-info"):
        assert single_demo.get(path).status_code == 401
    assert single_demo.get("/api/document/list", headers={"Host": "evil.example"}).status_code == 403
    assert single_demo.post("/api/auth/login", json={"password": "single-demo-password-123"},
                            headers={"Origin": "https://evil.example"}).status_code == 403

    response = single_demo.post(
        "/api/auth/login", json={"password": "single-demo-password-123"},
        headers={"Origin": "http://127.0.0.1"})
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "admin"
    assert "Secure" not in response.headers["set-cookie"]
    assert single_demo.get("/api/auth/me").json()["data"]["role"] == "admin"
    assert single_demo.get("/api/maintenance/restore-info").status_code == 200
    assert single_demo.post("/api/auth/logout").status_code == 200
    assert single_demo.get("/api/maintenance/restore-info").status_code == 401


def test_single_password_proxy_cookie_is_secure(single_demo):
    response = single_demo.post(
        "/api/auth/login", json={"password": "single-demo-password-123"},
        headers={"Origin": "http://127.0.0.1", "X-Forwarded-Proto": "https"})
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "admin"
    assert "Secure" in response.headers["set-cookie"]


def test_session_expiry_and_expensive_request_limits(demo):
    assert _login(demo, "guest-demo-password-123").status_code == 200
    token = demo.cookies.get(COOKIE_NAME)
    assert DEMO_AUTH.begin_work(token, "ocr")
    assert not DEMO_AUTH.begin_work(token, "ocr")
    DEMO_AUTH.end_work(token, "ocr")
    assert DEMO_AUTH.begin_work(token, "ocr")
    DEMO_AUTH.end_work(token, "ocr")
    key = next(iter(DEMO_AUTH._sessions))
    role, _ = DEMO_AUTH._sessions[key]
    DEMO_AUTH._sessions[key] = (role, time.monotonic() - 1)
    assert demo.get("/api/document/field-specs").status_code == 401


def test_login_throttle_before_password_hash_work(demo):
    DEMO_AUTH._failures = deque([time.monotonic()] * 20)
    response = _login(demo, "wrong-password")
    assert response.status_code == 429


def test_setup_script_preserves_other_env_and_disables_cleanly(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=keep-existing-value\n", encoding="utf-8")
    script = str(main.BASE_DIR / "scripts" / "setup_demo_auth.py")
    setup = subprocess.run(
        [sys.executable, script, "--origin", PUBLIC_ORIGIN,
         "--env-file", str(env_file)],
        capture_output=True, text=True, encoding="utf-8", check=True)
    configured = env_file.read_text(encoding="utf-8")
    assert "LLM_API_KEY=keep-existing-value" in configured
    assert "DOC_OCR_DEMO_MODE=1" in configured
    assert "DOC_OCR_DEMO_GUEST_HASH=pbkdf2_sha256$" in configured
    assert "访客临时口令" in setup.stdout
    assert "访客临时口令" not in configured
    before = dict(line.split("=", 1) for line in configured.splitlines() if "=" in line)
    rotated = subprocess.run(
        [sys.executable, script, "--guest-password", "demo guest 123",
         "--env-file", str(env_file)],
        capture_output=True, text=True, encoding="utf-8", check=True)
    updated = env_file.read_text(encoding="utf-8")
    after = dict(line.split("=", 1) for line in updated.splitlines() if "=" in line)
    assert after["DOC_OCR_DEMO_ADMIN_HASH"] == before["DOC_OCR_DEMO_ADMIN_HASH"]
    assert after["DOC_OCR_DEMO_GUEST_HASH"] != before["DOC_OCR_DEMO_GUEST_HASH"]
    assert verify_password("demo guest 123", after["DOC_OCR_DEMO_GUEST_HASH"])
    assert "demo guest 123" not in updated + rotated.stdout
    subprocess.run(
        [sys.executable, script, "--disable", "--env-file", str(env_file)],
        capture_output=True, text=True, encoding="utf-8", check=True)
    disabled = env_file.read_text(encoding="utf-8")
    assert "LLM_API_KEY=keep-existing-value" in disabled
    assert "DOC_OCR_DEMO_" not in disabled


def test_setup_script_single_password_writes_only_hash(tmp_path):
    env_file = tmp_path / ".env"
    script = str(main.BASE_DIR / "scripts" / "setup_demo_auth.py")
    result = subprocess.run(
        [sys.executable, script, "--origin", PUBLIC_ORIGIN,
         "--password", "single-demo-password-123", "--env-file", str(env_file)],
        capture_output=True, text=True, encoding="utf-8", check=True)
    configured = env_file.read_text(encoding="utf-8")
    assert "single-demo-password-123" not in configured + result.stdout
    assert "DOC_OCR_DEMO_GUEST_HASH" not in configured
    values = dict(line.split("=", 1) for line in configured.splitlines() if "=" in line)
    assert verify_password("single-demo-password-123", values["DOC_OCR_DEMO_ADMIN_HASH"])
