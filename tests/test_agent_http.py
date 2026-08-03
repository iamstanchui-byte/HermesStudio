"""Tests for v3.12.0 wrapper HTTPS support (`hermes_orch.agent_http`).

Covers:
- `get_verify()` honors env var precedence (default, INSECURE_SKIP_TLS_VERIFY,
  ORCHESTRATOR_CA_BUNDLE)
- `get_verify()` falls back gracefully when ORCHESTRATOR_CA_BUNDLE
  points at a missing file
- The thin `get`/`post`/`put`/`patch`/`delete` wrappers pass `verify=<resolved>`
  to httpx (verified by patching httpx.get/etc and inspecting kwargs)
- `agent_cli.py` no longer has any bare `httpx.{get,post,put,delete,patch}(`
  call sites (regression guard for the refactor)
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path
from unittest import mock

import pytest


# ----- helpers -----

def _reload_agent_http(env: dict[str, str]):
    """Reload hermes_orch.agent_http with a specific env. Returns the module."""
    # Save the relevant env keys we'll overwrite so we can restore later
    saved = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # Reload the module so its module-level _VERIFY is recomputed.
    if "hermes_orch.agent_http" in sys.modules:
        del sys.modules["hermes_orch.agent_http"]
    import hermes_orch.agent_http
    return hermes_orch.agent_http


@pytest.fixture
def restore_env():
    """Snapshot + restore the env vars we touch so tests don't leak."""
    keys = ["INSECURE_SKIP_TLS_VERIFY", "ORCHESTRATOR_CA_BUNDLE"]
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # Reload agent_http back to its post-saved state so the next test
    # starts clean
    if "hermes_orch.agent_http" in sys.modules:
        del sys.modules["hermes_orch.agent_http"]


# ----- 1) env var precedence -----

def test_default_verify_is_true(restore_env):
    """No env vars set -> verify=True (HTTPS with trusted CA, or plain HTTP)."""
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": None,
        "ORCHESTRATOR_CA_BUNDLE": None,
    })
    assert mod.get_verify() is True


def test_insure_skip_tls_verify_disables_verification(restore_env):
    """INSECURE_SKIP_TLS_VERIFY=1 -> verify=False (dev / self-signed)."""
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": "1",
        "ORCHESTRATOR_CA_BUNDLE": None,
    })
    assert mod.get_verify() is False


def test_insure_skip_accepts_truthy_values(restore_env):
    """'true', 'yes', 'on' all disable verification (per common conventions)."""
    for val in ("true", "TRUE", "yes", "YES", "on", "ON"):
        mod = _reload_agent_http({
            "INSECURE_SKIP_TLS_VERIFY": val,
            "ORCHESTRATOR_CA_BUNDLE": None,
        })
        assert mod.get_verify() is False, f"failed for value {val!r}"


def test_orchestrator_ca_bundle_wins_over_insecure(restore_env, tmp_path):
    """CA bundle takes precedence over INSECURE_SKIP_TLS_VERIFY (safer default
    when both are accidentally set)."""
    bundle = tmp_path / "ca.pem"
    bundle.write_text("---begin placeholder pem---\n", encoding="utf-8")
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": "1",
        "ORCHESTRATOR_CA_BUNDLE": str(bundle),
    })
    assert mod.get_verify() == str(bundle)


def test_orchestrator_ca_bundle_missing_file_falls_back(restore_env, tmp_path):
    """If ORCHESTRATOR_CA_BUNDLE points at a missing file, log a warning
    and fall through to the insecure-or-default branch."""
    missing = tmp_path / "does-not-exist.pem"
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": None,
        "ORCHESTRATOR_CA_BUNDLE": str(missing),
    })
    # Falls through to default since insecure isn't set
    assert mod.get_verify() is True


def test_orchestrator_ca_bundle_missing_with_insecure_set(restore_env, tmp_path):
    """If both are set but the bundle is missing, fall back to insecure."""
    missing = tmp_path / "nope.pem"
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": "1",
        "ORCHESTRATOR_CA_BUNDLE": str(missing),
    })
    assert mod.get_verify() is False


# ----- 2) thin wrappers pass verify=<resolved> -----

def test_get_wrapper_passes_verify_kwarg(restore_env):
    """`agent_http.get(url)` must invoke `httpx.get(url, verify=...)` with
    the resolved policy."""
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": "1",
        "ORCHESTRATOR_CA_BUNDLE": None,
    })
    with mock.patch.object(mod.httpx, "get", return_value=mock.Mock()) as m_get:
        mod.get("https://example.com/x")
    m_get.assert_called_once()
    args, kwargs = m_get.call_args
    # First positional arg is URL
    assert args[0] == "https://example.com/x"
    assert kwargs.get("verify") is False


def test_post_wrapper_passes_verify_kwarg(restore_env):
    """Same check for post (the most-used method in the wrapper)."""
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": "1",
        "ORCHESTRATOR_CA_BUNDLE": None,
    })
    with mock.patch.object(mod.httpx, "post", return_value=mock.Mock()) as m_post:
        mod.post("https://example.com/x", json={"a": 1}, timeout=10)
    m_post.assert_called_once()
    args, kwargs = m_post.call_args
    assert args[0] == "https://example.com/x"
    assert kwargs.get("verify") is False
    assert kwargs.get("json") == {"a": 1}
    assert kwargs.get("timeout") == 10


def test_put_wrapper_passes_verify_kwarg(restore_env):
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": None,
        "ORCHESTRATOR_CA_BUNDLE": None,
    })
    with mock.patch.object(mod.httpx, "put", return_value=mock.Mock()) as m_put:
        mod.put("https://example.com/x", content=b"x")
    args, kwargs = m_put.call_args
    assert kwargs.get("verify") is True


def test_delete_wrapper_passes_verify_kwarg(restore_env):
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": None,
        "ORCHESTRATOR_CA_BUNDLE": None,
    })
    with mock.patch.object(mod.httpx, "delete", return_value=mock.Mock()) as m_delete:
        mod.delete("https://example.com/x")
    args, kwargs = m_delete.call_args
    assert kwargs.get("verify") is True


def test_get_verify_callable_works(restore_env, tmp_path):
    """httpx.Client() calls in agent_cli.py use `verify=_agent_http_verify`
    (a re-import of `get_verify`). Make sure the function returns the
    right type so the kwarg passes cleanly. Regression guard for the
    issue where 4 httpx.Client sites bypassed the wrapper and crashed
    on self-signed certs."""
    mod = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": "1",
        "ORCHESTRATOR_CA_BUNDLE": None,
    })
    val = mod.get_verify()
    assert val is False
    # With a real CA bundle file, it should be the path string
    real_bundle = tmp_path / "ca.pem"
    real_bundle.write_text("--- placeholder ---\n", encoding="utf-8")
    mod2 = _reload_agent_http({
        "INSECURE_SKIP_TLS_VERIFY": None,
        "ORCHESTRATOR_CA_BUNDLE": str(real_bundle),
    })
    val2 = mod2.get_verify()
    assert val2 == str(real_bundle)


def test_agent_cli_uses_agent_http_verify_for_httpx_client():
    """Grep guard: every `httpx.Client(` call in agent_cli.py must
    pass `verify=` (typically `verify=_agent_http_verify()`). Otherwise
    the Client instance defaults to verify=True and rejects
    self-signed certs (the 4-site bug we hit on 2026-08-03).

    Note the trailing `()` — `_agent_http_verify` is a function reference
    (re-imported `get_verify`), so it MUST be called to get the actual
    value. Passing the function itself as `verify` makes httpx treat it
    as an SSL context callable, which crashes with
    "'function' object has no attribute 'set_alpn_protocols'".
    """
    p = Path(__file__).resolve().parent.parent / "src" / "hermes_orch" / "agent_cli.py"
    text = p.read_text(encoding="utf-8-sig")
    import re
    bad_no_verify = []
    bad_uncalled = []
    for m in re.finditer(r"httpx\.Client\(([^)]*)\)", text, re.DOTALL):
        args = m.group(1)
        if "verify" not in args:
            bad_no_verify.append(m.group(0)[:80])
        # Catch the bug: `verify=_agent_http_verify` without ()
        if re.search(r"verify=_agent_http_verify(?!\s*\()", args):
            bad_uncalled.append(m.group(0)[:80])
    assert not bad_no_verify, (
        f"agent_cli.py has httpx.Client() calls without `verify=`: {bad_no_verify}. "
        f"Add `verify=_agent_http_verify()` to honor ORCHESTRATOR_CA_BUNDLE / "
        f"INSECURE_SKIP_TLS_VERIFY."
    )
    assert not bad_uncalled, (
        f"agent_cli.py has httpx.Client() calls passing the function reference "
        f"instead of the result: {bad_uncalled}. Use `verify=_agent_http_verify()` "
        f"(with the parens) so httpx gets the resolved bool/string."
    )


# ----- 3) regression guard: agent_cli no longer has bare httpx call sites -----

def test_agent_cli_uses_aliased_httpx_only():
    """After the v3.12.0 refactor, no `httpx.<method>(` call sites should
    remain in agent_cli.py. If you add a new bare `httpx.post(` here, this
    test will fail and remind you to use the wrapper (which has the SSL
    policy baked in)."""
    p = Path(__file__).resolve().parent.parent / "src" / "hermes_orch" / "agent_cli.py"
    text = p.read_text(encoding="utf-8")
    # Look for `httpx.<method>(` patterns. The import line `import httpx`
    # is fine (just imports the module); what's not fine is a CALL
    # without the underscore-aliased wrapper.
    leftovers = re.findall(r"\bhttpx\.(get|post|put|delete|patch)\(", text)
    assert not leftovers, (
        f"agent_cli.py has {len(leftovers)} bare httpx call sites: {leftovers}. "
        f"Use `_httpx_get` / `_httpx_post` / etc. (the agent_http wrappers) "
        f"so the SSL verify policy is applied automatically."
    )
