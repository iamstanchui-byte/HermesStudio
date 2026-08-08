# coding: utf-8
"""Tests for the v1.0.1 `enroll` subcommand in agent_cli.

The `enroll` subcommand is a thin wrapper around POST /api/agents/enroll.
The atomicity / error-mapping logic is tested in
test_enrollment_api.py. Here we just verify the CLI is wired up:
  - The subcommand exists + has the expected options
  - Bad args (missing required flag) fail with a clean Click error
  - A bogus server URL fails with a clear connection error (not a
    Python traceback)
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from hermes_orch.agent_cli import cli


def test_enroll_command_is_registered():
    """`hermes-orch-agent enroll` is a subcommand of the cli group."""
    runner = CliRunner()
    result = runner.invoke(cli, ["enroll", "--help"])
    # If the command isn't registered, Click raises "No such command"
    assert result.exit_code == 0, f"unexpected exit: {result.output}\n{result.exception}"
    # The help output should mention the key options
    assert "--server" in result.output
    assert "--token" in result.output
    assert "--agent-name" in result.output


def test_enroll_requires_token_flag():
    """Missing --token should fail with a clear Click usage error."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["enroll", "--server", "http://x", "--agent-name", "a"]
    )
    # Click returns exit code 2 for usage errors
    assert result.exit_code == 2
    assert "Missing option" in result.output or "--token" in result.output


def test_enroll_requires_agent_name_flag():
    """Missing --agent-name should fail with a clear Click usage error."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["enroll", "--server", "http://x", "--token", "etok-fake"]
    )
    assert result.exit_code == 2
    assert "Missing option" in result.output or "--agent-name" in result.output


def test_enroll_requires_server_flag():
    """Missing --server should fail with a clear Click usage error."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["enroll", "--token", "etok-fake", "--agent-name", "a"]
    )
    assert result.exit_code == 2
    assert "Missing option" in result.output or "--server" in result.output


def test_enroll_with_unreachable_server_cleanly_errors():
    """Connection refused → exit 1 with a clear error (not a traceback)."""
    runner = CliRunner()
    # 127.0.0.1:1 is reserved (tcpmux) and effectively never bound
    # for our use. The enroll command should fail with a clean
    # error message, not a Python traceback.
    result = runner.invoke(
        cli,
        [
            "enroll",
            "--server", "http://127.0.0.1:1",
            "--token", "etok-fake",
            "--agent-name", "test-agent",
        ],
    )
    # Exit 1 (Click RuntimeError from our sys.exit(1) on connection
    # failure). exit 0 = success, exit 2 = Click usage error.
    assert result.exit_code == 1
    assert "cannot reach" in result.output.lower() or "ERROR" in result.output
    # No Python traceback in the output (defensive: a real bug
    # would show the full traceback here)
    assert "Traceback" not in result.output
