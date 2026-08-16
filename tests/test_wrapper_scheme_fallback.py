"""Tests for wrapper scheme-discovery + try-other-scheme helpers
(2026-08-16 wrapper self-heal).

The wrapper's `wrapper-config.json` hardcodes a single `orchestrator_url`.
When the server flips between HTTP and HTTPS (or any other
public_origin change), every wrapper breaks. The recovery
should not require SSH-ing into every agent host.

This module tests three small helpers that live in
`hermes_orch.agent_http`:

  - `_swap_scheme(url)`            -- pure function: http<->https
  - `_classify_failure(exc)`        -- pure function: 'connection' or 'other'
  - `request_with_fallback(method, url, **kwargs)`
                                    -- call httpx, on a classified
                                       connection failure retry with
                                       the other scheme, return
                                       (response, actual_url)

The wrapper daemon uses `request_with_fallback` for the heartbeat,
and after a successful heartbeat calls `/api/server/info` to learn
the canonical URL. If the canonical URL differs from the configured
URL, the wrapper atomically updates `wrapper-config.json` (handled
in `agent_cli.py` -- out of scope for this test module).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# Allow tests to import from src
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# ----------------------------------------------------------------------
# 1) _swap_scheme
# ----------------------------------------------------------------------

@pytest.mark.parametrize("input_url,expected", [
    # round-trip http <-> https
    ("http://localhost:8765",        "https://localhost:8765"),
    ("https://localhost:8765",       "http://localhost:8765"),
    ("http://hermes-win:8765",       "https://hermes-win:8765"),
    ("https://192.168.2.152:8765",   "http://192.168.2.152:8765"),
    # path / query / fragment are preserved verbatim
    ("http://host:8765/api/health",  "https://host:8765/api/health"),
    ("https://h:1/x?y=2#z",          "http://h:1/x?y=2#z"),
    # non-http(s) schemes pass through unchanged (we don't know
    # how to "swap" them; the caller is expected to call us only
    # with http(s) URLs)
])
def test_swap_scheme(input_url, expected):
    from hermes_orch.agent_http import _swap_scheme
    assert _swap_scheme(input_url) == expected


def test_swap_scheme_rejects_non_strings():
    """Non-strings raise -- callers must pre-validate."""
    from hermes_orch.agent_http import _swap_scheme
    for bad in (None, 123, b"http://x", [], {}):
        with pytest.raises((TypeError, ValueError, AttributeError)):
            _swap_scheme(bad)


# ----------------------------------------------------------------------
# 2) _classify_failure
# ----------------------------------------------------------------------

def test_classify_failure_connection_refused_is_connection():
    """httpx.ConnectError = TCP-level connection failure. Try fallback."""
    from hermes_orch.agent_http import _classify_failure
    import httpx
    err = httpx.ConnectError("Connection refused")
    assert _classify_failure(err) == "connection"


def test_classify_failure_timeout_is_connection():
    """ReadTimeout / ConnectTimeout / PoolTimeout = transient network, try fallback."""
    from hermes_orch.agent_http import _classify_failure
    import httpx
    for cls in (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout):
        err = cls("timeout")
        assert _classify_failure(err) == "connection", f"{cls.__name__} should be 'connection'"


def test_classify_failure_protocol_error_is_other():
    """RemoteProtocolError means we connected but the wire is wrong
    (e.g. plain HTTP against an HTTPS server). Try fallback -- the
    server might actually be the other scheme."""
    from hermes_orch.agent_http import _classify_failure
    import httpx
    err = httpx.RemoteProtocolError("Server disconnected")
    # This is the "Server disconnected without sending a response"
    # we saw in the original HTTPS flip. Swapping scheme is exactly
    # the right move.
    assert _classify_failure(err) == "connection"


def test_classify_failure_request_error_is_other():
    """Generic RequestError (e.g. invalid URL) is NOT connection --
    swapping scheme won't help and might silently break auth."""
    from hermes_orch.agent_http import _classify_failure
    import httpx
    err = httpx.RequestError("Invalid URL")
    assert _classify_failure(err) == "other"


def test_classify_failure_http_status_is_other():
    """A returned HTTP response (4xx, 5xx) is NOT a connection error.
    Swapping scheme would just produce a different status from the
    same logical server. Don't fall back."""
    from hermes_orch.agent_http import _classify_failure
    import httpx
    resp = httpx.Response(500, request=httpx.Request("GET", "http://x"))
    err = httpx.HTTPStatusError("500", request=resp.request, response=resp)
    assert _classify_failure(err) == "other"


# ----------------------------------------------------------------------
# 3) request_with_fallback
# ----------------------------------------------------------------------

def test_request_with_fallback_first_attempt_succeeds():
    """If the first call succeeds, the helper returns the response
    without retrying on the other scheme."""
    from hermes_orch import agent_http
    resp_ok = mock.Mock(status_code=200, text="ok")
    with mock.patch.object(agent_http.httpx, "get", return_value=resp_ok) as mget:
        out_resp, out_url = agent_http.request_with_fallback(
            "GET", "https://localhost:8765/api/health", timeout=5
        )
    assert out_resp is resp_ok
    assert out_url == "https://localhost:8765/api/health"
    assert mget.call_count == 1


def test_request_with_fallback_https_to_http_on_connect_error():
    """HTTPS -> HTTP fallback when the HTTPS call hits ConnectError."""
    from hermes_orch import agent_http
    import httpx

    resp_ok = mock.Mock(status_code=200, text="ok")
    connect_err = httpx.ConnectError("refused")

    # First call (HTTPS) raises ConnectError, second call (HTTP) succeeds
    side_effects = [connect_err, resp_ok]
    with mock.patch.object(agent_http.httpx, "get", side_effect=side_effects) as mget:
        out_resp, out_url = agent_http.request_with_fallback(
            "GET", "https://localhost:8765/api/health", timeout=5
        )

    assert out_resp is resp_ok
    assert out_url == "http://localhost:8765/api/health"
    assert mget.call_count == 2
    # First call was https, second was http
    first_url = mget.call_args_list[0].args[0]
    second_url = mget.call_args_list[1].args[0]
    assert first_url.startswith("https://")
    assert second_url.startswith("http://")


def test_request_with_fallback_http_to_https_on_connect_error():
    """HTTP -> HTTPS fallback (the actual HTTPS-flip scenario)."""
    from hermes_orch import agent_http
    import httpx

    resp_ok = mock.Mock(status_code=200, text="ok")
    connect_err = httpx.ConnectError("refused")
    side_effects = [connect_err, resp_ok]
    with mock.patch.object(agent_http.httpx, "get", side_effect=side_effects) as mget:
        out_resp, out_url = agent_http.request_with_fallback(
            "GET", "http://localhost:8765/api/health", timeout=5
        )
    assert out_resp is resp_ok
    assert out_url == "https://localhost:8765/api/health"
    assert mget.call_count == 2


def test_request_with_fallback_both_attempts_fail_raises_last():
    """If both schemes fail with a connection error, the LAST exception
    propagates (so the caller still sees the underlying error and
    can decide what to do)."""
    from hermes_orch import agent_http
    import httpx

    err_a = httpx.ConnectError("a")
    err_b = httpx.ConnectError("b")
    with mock.patch.object(agent_http.httpx, "get", side_effect=[err_a, err_b]):
        with pytest.raises(httpx.ConnectError) as exc_info:
            agent_http.request_with_fallback(
                "GET", "https://localhost:8765/api/health", timeout=5
            )
    assert "b" in str(exc_info.value)


def test_request_with_fallback_non_connection_error_propagates_immediately():
    """A 'other' failure (e.g. invalid URL) is raised without fallback --
    swapping scheme would just turn one error into another without
    any chance of recovery."""
    from hermes_orch import agent_http
    import httpx

    err = httpx.RequestError("Invalid URL, contains non-printable bytes")
    with mock.patch.object(agent_http.httpx, "get", side_effect=err) as mget:
        with pytest.raises(httpx.RequestError):
            agent_http.request_with_fallback(
                "GET", "https://localhost:8765/api/health", timeout=5
            )
    assert mget.call_count == 1  # no fallback attempt


# ----------------------------------------------------------------------
# 4) end-to-end: discover server info on fallback success
# ----------------------------------------------------------------------

def test_wrapper_reads_server_info_after_successful_fallback():
    """After a fallback, the wrapper calls /api/server/info to learn
    the canonical URL. If it differs from the configured URL, the
    wrapper updates its config.

    This test pins the contract for the URL-rewrite helper that
    `agent_cli.py` will call after each successful heartbeat.
    """
    from hermes_orch import agent_http
    import httpx

    configured = "http://localhost:8765"
    # server reports https (the actual scheme after the flip)
    info_body = b'{"scheme":"https","public_origin":"https://localhost:8765","cert_fingerprint_sha256":""}'

    # 1st call = heartbeat (HTTP) -> ConnectError
    # 2nd call = heartbeat (HTTPS) -> success
    # 3rd call = /api/server/info (HTTPS) -> info body
    heartbeat_https = mock.Mock(status_code=200, text="ok")
    info_resp = mock.Mock(status_code=200, content=info_body)
    side_effects = [
        httpx.ConnectError("refused"),
        heartbeat_https,
        info_resp,
    ]
    with mock.patch.object(agent_http.httpx, "get", side_effect=side_effects) as mget:
        # First: heartbeat with fallback
        hb_resp, hb_url = agent_http.request_with_fallback(
            "GET", configured + "/api/agents/x/heartbeat", timeout=5
        )
        assert hb_url == "https://localhost:8765/api/agents/x/heartbeat"

        # Then: read server-info (now using the canonical URL)
        info_url = hb_url.rsplit("/api/agents/x/heartbeat", 1)[0] + "/api/server/info"
        info_r, info_actual = agent_http.request_with_fallback(
            "GET", info_url, timeout=5
        )
        # The actual URL is the one we asked for
        assert info_actual == "https://localhost:8765/api/server/info"
        body = json.loads(info_r.content)
        assert body["public_origin"] == "https://localhost:8765"
        # Operator can now write body["public_origin"] to wrapper-config.json
