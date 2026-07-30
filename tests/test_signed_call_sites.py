"""Meta-test: walk every _auth_headers / _hmac_headers call site in
agent_cli.py and assert the path argument is not a literal string
containing `{...}` template syntax (v1.9 / v1.9.1 lesson).

If the path is a literal like '/api/tasks/{task_id}/start' (no
f-string), the signature is bound to the wrong path → server 401s.
The runtime symptom is "wrapper can't claim/result/cleanup tasks"
and the user has to find the buggy call site in a 166k-line file.

This test makes that class of bug impossible to introduce without
a red CI signal.
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
