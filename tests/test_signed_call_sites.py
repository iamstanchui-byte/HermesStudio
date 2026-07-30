"""Meta-test: walk every _auth_headers / _hmac_headers call site in
agent_cli.py and assert the path argument is not a literal string
containing `{...}` template syntax (v1.9 / v1.9.1 lesson).

If the path is a literal like '/api/tasks/{task_id}/start' (no
f-string), the signature is bound to the wrong path → server 401s.
The runtime symptom is "wrapper can't claim/result/cleanup tasks"
and the user has to find the buggy call site in a 166k-line file.

This test makes that class of bug impossible to introduce without
a red CI signal.

v1.9.3 also adds a body-signing meta-test (`json=` next to
`_auth_headers` without a body arg). Same root cause class as the
path-template bug, but a different instance: the wrapper was
signing `body=b""` (the default) while the server saw the actual
JSON body. Symptom: hermes finished, the wrapper submitted, and
the server 401'd. Task stayed in 'running' for 3 minutes until
the supervisor's stuck_wrapper check marked it failed. User saw
"task timeout failed" but the root cause was a signing bug.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src" / "hermes_orch" / "agent_cli.py"


# Function names that sign a wrapper request. The path arg is the
# 2nd positional (for _auth_headers) or the `path=` kwarg (for
# _hmac_headers).
SIGNING_FUNCS = {"_auth_headers", "_hmac_headers"}


def _is_literal_with_template(node: ast.expr) -> bool:
    """True iff `node` is a `ast.Constant` whose value is a string
    containing `{...}` (template syntax). The bug we're guarding
    against is exactly this: a path like '/api/tasks/{task_id}/start'
    (no `f` prefix) signed as-is.
    """
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    # Match `{...}` template syntax. Not the same as f-string which
    # would parse to ast.JoinedStr.
    return bool(re.search(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", node.value))


def _collect_signing_calls(tree: ast.AST) -> list[tuple[str, int, str, ast.expr]]:
    """Return [(func_name, line, raw_arg_repr, path_node)] for every
    signing call where the path is the 2nd positional arg or a `path=`
    keyword arg.
    """
    out: list[tuple[str, int, str, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name) or func.id not in SIGNING_FUNCS:
            continue
        # Find the path arg: 2nd positional OR `path=` keyword
        path_node: ast.expr | None = None
        if node.args and len(node.args) >= 2:
            path_node = node.args[1]
        else:
            for kw in node.keywords:
                if kw.arg == "path":
                    path_node = kw.value
                    break
        if path_node is None:
            continue
        # ast.unparse to show the literal source
        try:
            raw = ast.unparse(path_node)
        except Exception:
            raw = "<unparse failed>"
        out.append((func.id, node.lineno, raw, path_node))
    return out


@pytest.fixture(scope="module")
def signing_calls():
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    return _collect_signing_calls(tree)


def test_no_literal_template_path_in_signing_calls(signing_calls):
    """No signing call should pass a literal `{...}` template as the
    path. Must be either an f-string (JoinedStr) or a variable
    bound to one. This is the bug that caused cleanup-ack to 401
    since v1.6 and claim/result to 401 since v1.6 too.
    """
    bad: list[tuple[str, int, str]] = []
    for func, line, raw, path_node in signing_calls:
        if _is_literal_with_template(path_node):
            bad.append((func, line, raw))
    assert not bad, (
        f"Found {len(bad)} signing call(s) with literal `{{...}}` path templates. "
        f"Server's require_hmac_auth verifies the signature over the actual URL "
        f"path; signing a literal template (no f-string) → 401 on every call. "
        f"Use a variable: `_p = f'...{{var}}...'; headers=_auth_headers(M, _p)`. "
        f"Offenders:\n"
        + "\n".join(f"  {func} (line {line}): {raw!r}" for func, line, raw in bad)
    )


def test_all_signing_calls_have_path_arg(signing_calls):
    """Every _auth_headers / _hmac_headers call must have a path
    arg (positional 2nd or `path=` keyword). Catches accidental
    refactors that drop the arg.
    """
    for func, line, raw, path_node in signing_calls:
        assert path_node is not None, (
            f"{func} at line {line} has no path arg"
        )


def test_at_least_one_signing_call(signing_calls):
    """Sanity: we should be finding signing calls in agent_cli.py.
    If this fails, either the file was deleted or the regex above
    got out of sync. Catches the meta-test silently passing
    because there was nothing to test.
    """
    assert len(signing_calls) > 10, (
        f"Only found {len(signing_calls)} signing calls — expected many more. "
        f"File may have been refactored; review the SIGNING_FUNCS list."
    )


# ===== v1.9.3 body-signing meta-test =====
#
# Bug class: httpx.post/put(..., json=X, headers=_auth_headers(M, path))
# where _auth_headers is called without a `body=` arg. httpx's `json=`
# kwarg encodes X to JSON bytes internally, but the signature was
# computed over `body=b""`. Server hashes the actual JSON body → 401.
#
# Why a separate meta-test: this is a different axis from the
# path-template bug. The path-template bug is in the SECOND arg of
# _auth_headers; the body-signing bug is in the THIRD arg being
# missing. We can catch both in one walk, but keeping them as
# separate test functions gives clearer error messages on failure.


def _call_has_json_kwarg(call: ast.Call) -> bool:
    """True if `call` has a `json=` keyword argument."""
    for kw in call.keywords:
        if kw.arg == "json":
            return True
    return False


def _call_has_content_kwarg(call: ast.Call) -> bool:
    """True if `call` has a `content=` keyword argument (preferred
    for HMAC-signed requests so we control the exact body bytes)."""
    for kw in call.keywords:
        if kw.arg == "content":
            return True
    return False


def _extract_signing_call_from_headers(
    headers_node: ast.expr | None,
) -> tuple[str, ast.Call] | None:
    """If `headers_node` is a direct call to _auth_headers /
    _hmac_headers, return (func_name, call_node). Otherwise None.

    This only handles the case where `headers=` is the call
    itself, e.g. `headers=_auth_headers('POST', path, body)`. If
    someone wraps it (e.g. `headers={**_auth_headers(...)}`), this
    returns None and we don't enforce the body arg — that's
    deliberate, we don't want to over-constrain.
    """
    if headers_node is None or not isinstance(headers_node, ast.Call):
        return None
    func = headers_node.func
    if not isinstance(func, ast.Name) or func.id not in SIGNING_FUNCS:
        return None
    return (func.id, headers_node)


def _signing_call_has_body_arg(call: ast.Call) -> bool:
    """True if `_auth_headers`/`_hmac_headers` call has a body arg
    (either 3rd positional or `body=` keyword)."""
    if len(call.args) >= 3:
        return True
    for kw in call.keywords:
        if kw.arg == "body":
            return True
    return False


def _collect_json_with_signing_calls(tree: ast.AST) -> list[tuple[int, str, str, bool]]:
    """Walk every httpx.post/httpx.put call. For each, if it has
    BOTH a `json=` kwarg AND a direct `_auth_headers`/`_hmac_headers`
    call in `headers=`, return:
        (line, method, signing_func, signing_call_has_body)
    where `signing_call_has_body` is True iff the signing call has
    a body arg.

    A True `signing_call_has_body` is correct (the bug would be
    False). We return all such calls so the test can assert
    `signing_call_has_body` is True everywhere.
    """
    out: list[tuple[int, str, str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match `httpx.post(...)`, `httpx.put(...)`, `client.post(...)`,
        # `client.put(...)`. We don't try to handle every wrapper
        # pattern; the daemon uses `httpx.post/put` directly.
        method_name: str | None = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "httpx" and func.attr in ("post", "put"):
                method_name = func.attr
        if method_name is None:
            continue
        if not _call_has_json_kwarg(node):
            continue
        # Find headers= kwarg
        headers_node: ast.expr | None = None
        for kw in node.keywords:
            if kw.arg == "headers":
                headers_node = kw.value
                break
        sig = _extract_signing_call_from_headers(headers_node)
        if sig is None:
            continue
        _, signing_call = sig
        out.append((
            node.lineno,
            method_name,
            sig[0],
            _signing_call_has_body_arg(signing_call),
        ))
    return out


@pytest.fixture(scope="module")
def json_with_signing_calls():
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    return _collect_json_with_signing_calls(tree)


def test_no_json_with_unsigned_signing_call(json_with_signing_calls):
    """Every `httpx.post/put(..., json=X, headers=_auth_headers(...))`
    call must have the signing call pass a body arg. Otherwise the
    signature is computed over `body=b""` while the server hashes
    the actual JSON body → 401.

    This is the v1.9.3 bug class. The runtime symptom is: hermes
    finishes, the wrapper submits the result, server 401s, the
    task stays in 'running' for 3 minutes until the supervisor's
    stuck_wrapper check fires. The user sees "task timeout failed"
    but the root cause is a signing bug, not a hermes hang.

    Recommended fix: serialize the body once, pass it as `body=`
    to the signing call, and use `content=` (not `json=`) on the
    httpx call so the body bytes are exactly what was signed.
    """
    bad: list[tuple[int, str, str]] = []
    for line, method, sig_func, has_body in json_with_signing_calls:
        if not has_body:
            bad.append((line, method, sig_func))
    assert not bad, (
        f"Found {len(bad)} httpx.{method} call(s) using `json=` next to a "
        f"signing call without a `body=` arg. The signature is bound to "
        f"`body=b\"\"` while the server hashes the actual JSON body, so "
        f"every call 401s. Fix: serialize the body once, pass it as `body=` "
        f"to the signing call, and use `content=` (not `json=`) on the "
        f"httpx call. Offenders:\n"
        + "\n".join(
            f"  line {line}: httpx.{method}(... json=... headers={sig_func}(...) without body arg"
            for line, method, sig_func in bad
        )
    )
