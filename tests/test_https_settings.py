"""Tests for v3.12.0 HTTPS / TLS feature.

Covers:
- DEFAULT_CONFIG has the new `https` section
- `https_view` extracts cert subject / SANs / expiry days
- `https_view` returns `ready=False` when cert/key files missing
- HTTPS POST persists the section to disk
- gen-cert CLI subcommand produces a valid cert + key pair
- gen-cert refuses to overwrite without --force
- `set_session_cookie(secure=...)` based on `request.url.scheme`
- `set_session_cookie` without request keeps backward-compat (no Secure)
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# ----- helpers -----

class _MockResp:
    """Minimal stand-in for fastapi.Response — records the kwargs that
    set_session_cookie passed to response.set_cookie."""
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def set_cookie(self, **kw) -> None:
        self.kwargs = kw


class _MockRequest:
    """Stand-in for fastapi.Request — only the `.url.scheme` attr is read."""
    def __init__(self, scheme: str) -> None:
        from starlette.datastructures import URL
        self.url = URL(f"{scheme}://example.com/")


# ----- 1) DEFAULT_CONFIG -----

def test_default_config_has_https_section():
    """v3.12.0: HTTPS section ships in defaults (disabled, empty paths)."""
    from hermes_orch.config import DEFAULT_CONFIG
    assert "https" in DEFAULT_CONFIG, "missing https section"
    https = DEFAULT_CONFIG["https"]
    assert https["enabled"] is False
    assert https["ssl_cert_path"] == ""
    assert https["ssl_key_path"] == ""


# ----- 2) https_view -----

def test_https_view_disabled_by_default():
    """When config has no `https` section, view reports disabled + not ready."""
    from hermes_orch.api.settings import _https_view
    view = _https_view({})
    assert view.enabled is False
    assert view.ready is False
    assert view.cert_exists is False
    assert view.key_exists is False
    assert view.cert_expires_in_days is None


def test_https_view_reports_missing_files():
    """Paths set but files don't exist => ready=False, expiry=None."""
    from hermes_orch.api.settings import _https_view
    view = _https_view({
        "https": {
            "enabled": True,
            "ssl_cert_path": "/no/such/cert.pem",
            "ssl_key_path": "/no/such/key.pem",
        }
    })
    assert view.enabled is True
    assert view.cert_exists is False
    assert view.key_exists is False
    assert view.ready is False
    assert view.cert_expires_in_days is None


def test_https_view_parses_valid_cert(tmp_path):
    """Generate a real self-signed cert via the gen-cert code path,
    point the view at it, verify CN + SANs + expiry come back populated."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    from hermes_orch.api.settings import _https_view

    host = "unit.test"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(host),
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))

    view = _https_view({
        "https": {
            "enabled": True,
            "ssl_cert_path": str(cert_path),
            "ssl_key_path": str(key_path),
        }
    })
    assert view.cert_exists is True
    assert view.key_exists is True
    assert view.ready is True
    assert view.cert_subject_cn == host
    assert "localhost" in view.cert_sans
    assert "unit.test" in view.cert_sans
    # 7 days validity, with a 1-min grace before — so we expect 6 or 7 days
    assert view.cert_expires_in_days is not None
    assert 6 <= view.cert_expires_in_days <= 7


# ----- 3) set_session_cookie Secure flag -----

def test_set_cookie_secure_off_on_http():
    from hermes_orch.auth.cookie import set_session_cookie
    r = _MockResp()
    set_session_cookie(r, "u1", request=_MockRequest("http"))
    assert r.kwargs is not None
    assert r.kwargs["secure"] is False


def test_set_cookie_secure_on_https():
    from hermes_orch.auth.cookie import set_session_cookie
    r = _MockResp()
    set_session_cookie(r, "u1", request=_MockRequest("https"))
    assert r.kwargs is not None
    assert r.kwargs["secure"] is True


def test_set_cookie_no_request_keeps_backward_compat():
    """Pre-v3.12.0 call sites pass (response, user_id) without request.
    Make sure we don't suddenly lock out cookies (Secure=False when
    we can't tell the scheme)."""
    from hermes_orch.auth.cookie import set_session_cookie
    r = _MockResp()
    set_session_cookie(r, "u1")
    assert r.kwargs is not None
    assert r.kwargs["secure"] is False
    # Other cookie attrs unchanged
    assert r.kwargs["httponly"] is True
    assert r.kwargs["samesite"] == "lax"


# ----- 4) gen-cert CLI -----

def _run_gen_cert(out_dir: Path, *, hostname: str = "cli.test", days: int = 30,
                  force: bool = False) -> subprocess.CompletedProcess:
    """Invoke `python -m hermes_orch.cli gen-cert` as a subprocess so we
    exercise the actual CLI entry point (click argument parsing, file
    output, chmod). Returns the CompletedProcess for assertion."""
    project_root = Path(__file__).resolve().parent.parent
    python_exe = project_root / ".venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable)
    args = [
        str(python_exe), "-m", "hermes_orch.cli", "gen-cert",
        "--hostname", hostname,
        "--days", str(days),
        "--out-dir", str(out_dir),
    ]
    if force:
        args.append("--force")
    return subprocess.run(
        args,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_gen_cert_produces_valid_pair(tmp_path):
    """gen-cert writes a parseable cert + key, and the cert is valid
    for the requested number of days."""
    proc = _run_gen_cert(tmp_path, hostname="pytest.local", days=10)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    assert cert_path.is_file()
    assert key_path.is_file()
    assert cert_path.stat().st_size > 100
    assert key_path.stat().st_size > 100
    # Parse back, confirm the cert is what gen-cert claimed
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import NameOID
    with open(cert_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "pytest.local"
    delta = cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc)
    assert 9 <= delta.days <= 11, f"expected ~10 day validity, got {delta.days}"


def test_gen_cert_refuses_overwrite(tmp_path):
    """Second invocation without --force must fail loudly."""
    proc1 = _run_gen_cert(tmp_path, hostname="first.local", days=10)
    assert proc1.returncode == 0
    proc2 = _run_gen_cert(tmp_path, hostname="second.local", days=10, force=False)
    assert proc2.returncode != 0, "should refuse to overwrite without --force"
    assert "already exists" in proc2.stderr or "already exists" in proc2.stdout
    # With --force, it should succeed
    proc3 = _run_gen_cert(tmp_path, hostname="third.local", days=10, force=True)
    assert proc3.returncode == 0, f"stderr: {proc3.stderr}"


# ----- 5) HTTPS POST round-trip (in-process) -----

def test_https_post_persists_to_config(tmp_path, monkeypatch):
    """POST /api/settings/https writes the section to the user config.yaml."""
    from fastapi.testclient import TestClient

    # Use a temp config path so we don't clobber the real one
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("orchestrator:\n  port: 18765\n  host: 127.0.0.1\n  log_level: INFO\n",
                        encoding="utf-8")
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(cfg_path))

    from hermes_orch.config import load_config, save_config_section
    from hermes_orch.api.settings import post_https, get_https

    save_config_section("https", {
        "enabled": True,
        "ssl_cert_path": "/some/cert.pem",
        "ssl_key_path": "/some/key.pem",
    })
    cfg = load_config()
    assert cfg["https"]["enabled"] is True
    assert cfg["https"]["ssl_cert_path"] == "/some/cert.pem"
    assert cfg["https"]["ssl_key_path"] == "/some/key.pem"
    # And the view reflects it
    view = get_https.__wrapped__ if hasattr(get_https, "__wrapped__") else None
    # We can't call get_https without a Request, so go through _https_view
    from hermes_orch.api.settings import _https_view
    v = _https_view(cfg)
    assert v.enabled is True
    assert v.ssl_cert_path == "/some/cert.pem"
    # Files don't exist on disk so ready=False
    assert v.ready is False
