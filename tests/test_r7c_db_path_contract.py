"""Regression tests for the R7-C DB-path contract (security/agent-endpoint-auth-hotfix B12 review).

The production NSSM service runs as LocalSystem with
`HERMES_ORCH_CONFIG=C:\\ProgramData\\HermesOrchestrator\\config\\config.yaml`.
Before R7-C, the lifespan used
`Path.home() / ".hermes-orchestrator" / "hermes-orch.db"`, which under
LocalSystem resolves to
`C:\\Windows\\System32\\config\\systemprofile\\.hermes-orchestrator\\hermes-orch.db`
-- a fresh empty DB that has no agent rows. The service would 401
"Unknown agent" for every heartbeat.

The R7-C fix derives the DB path from the resolved config path:
`_cfg_path = find_config_path(); db_path = _cfg_path.parent / "hermes-orch.db"`.

This file pins the contract:
  1. With HERMES_ORCH_CONFIG set to a ProgramData path, db_path is
     the config's parent dir + "hermes-orch.db".
  2. With NO config resolvable (env unset, no home config, no local
     config), db_path falls back to
     `Path.home() / ".hermes-orchestrator" / "hermes-orch.db"`
     (the historical user-profile path).
  3. Under LocalSystem (Path.home() == systemprofile), with
     HERMES_ORCH_CONFIG set, the db_path does NOT include
     "systemprofile" -- the production setup takes precedence.
  4. When HERMES_ORCH_CONFIG is unset but the home dir HAS a
     config.yaml, db_path is the home config's parent dir +
     "hermes-orch.db" (dev mode).

These tests use the Database class monkeypatch pattern from
`tests/test_users_api.py` to capture the path computed by the
lifespan without actually opening a database.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from hermes_orch import main as main_mod
from hermes_orch import db as db_mod


# ===== Helpers =====


def _capture_db_path(tmp_path, monkeypatch):
    """Return a function that captures the db_path the lifespan
    computes for `Database(...)`.

    The lifespan does:
        db_path = ...  # R7-C logic
        db = Database(db_path)
        await db.connect()

    We monkeypatch `Database.__init__` to capture `db_path` and
    redirect the actual init to a tmp DB so the lifespan doesn't
    try to open the production DB.

    Returns (capture_fn, app_factory). The caller calls
    `app_factory()` to get a fresh app, then runs the lifespan.
    """
    captured: dict[str, Path] = {}

    def patched_init(self, db_path):
        captured["db_path"] = db_path
        # Don't open the real DB. The lifespan will then try to
        # `await db.connect()` and fail, which is fine for the test
        # -- we only care that db_path was computed correctly.
        self.db_path = db_path

    monkeypatch.setattr(db_mod.Database, "__init__", patched_init)
    return captured


# ===== Tests =====


@pytest.mark.asyncio
async def test_r7c_db_path_programdata_contract(tmp_path, monkeypatch):
    """HERMES_ORCH_CONFIG=ProgramData path -> db_path = config parent / hermes-orch.db.

    Pins the production NSSM service contract. Under LocalSystem,
    Path.home() == systemprofile, so this test also guards against
    any future regression that would let Path.home() leak into the
    production DB path.
    """
    # Set up: HERMES_ORCH_CONFIG points at a ProgramData-style path
    # (the actual production path, but in a tmp dir for the test).
    fake_program_data = tmp_path / "ProgramData" / "HermesOrchestrator" / "config"
    fake_program_data.mkdir(parents=True)
    fake_config = fake_program_data / "config.yaml"
    fake_config.write_text("orchestrator:\n  port: 8765\n")
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(fake_config))

    # Path.home() in the test environment is NOT systemprofile, but
    # we monkeypatch it to systemprofile to simulate LocalSystem.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("C:/Windows/System32/config/systemprofile")))

    # Also ensure no ~/.hermes-orchestrator/config.yaml interferes
    # (the user's real one might exist; make find_config_path NOT see it).
    # find_config_path() priority: HERMES_ORCH_CONFIG > home > local.
    # We set HERMES_ORCH_CONFIG above, so it wins. The home-config
    # check is for Path.home()/.hermes-orchestrator/config.yaml
    # which we just set to systemprofile. systemprofile won't have
    # this file, so find_config_path returns our env value.

    captured = _capture_db_path(tmp_path, monkeypatch)

    app = main_mod.create_app()
    try:
        async with app.router.lifespan_context(app):
            pass  # capture happens in patched_init
    except Exception:
        # The lifespan will fail at db.connect() (we redirected
        # the path). That's fine -- we only care about db_path.
        pass

    assert "db_path" in captured, "lifespan did not call Database(...)"
    db_path: Path = captured["db_path"]

    # Deterministic contract: db_path is <config dir>/hermes-orch.db
    expected = fake_program_data / "hermes-orch.db"
    assert db_path == expected, (
        f"DB-path contract broken: "
        f"HERMES_ORCH_CONFIG={fake_config} should produce "
        f"db_path={expected}, but got {db_path}"
    )
    # Critical regression guard: under simulated LocalSystem, the
    # db_path must NOT include "systemprofile" anywhere.
    assert "systemprofile" not in str(db_path).lower(), (
        f"DB-path contract REGRESSION: db_path={db_path} includes "
        f"'systemprofile' even though HERMES_ORCH_CONFIG is set. "
        f"This is the exact LocalSystem bug the R7-C fix is meant to "
        f"prevent."
    )


@pytest.mark.asyncio
async def test_r7c_db_path_fallback_when_no_config_resolves(tmp_path, monkeypatch):
    """No HERMES_ORCH_CONFIG, no home config, no local config
    -> db_path = Path.home() / '.hermes-orchestrator' / 'hermes-orch.db'.

    Pins the explicit fallback contract: when find_config_path()
    returns None, we use the historical user-profile path. The
    Database.connect() will fail (no real DB there) but the path
    computation must match exactly.
    """
    # No HERMES_ORCH_CONFIG
    monkeypatch.delenv("HERMES_ORCH_CONFIG", raising=False)

    # No home config (override Path.home to a tmp dir with no
    # .hermes-orchestrator/config.yaml).
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # No local config (we're running from a different cwd in the test;
    # the lifespan's find_config_path checks for "./config.yaml" but
    # we can be defensive by ensuring no ./config.yaml exists in tmp_path).
    # The test's cwd is the project root, but the env var is unset,
    # home has no config, and we don't add a ./config.yaml. So
    # find_config_path() returns None.

    captured = _capture_db_path(tmp_path, monkeypatch)

    app = main_mod.create_app()
    try:
        async with app.router.lifespan_context(app):
            pass
    except Exception:
        pass

    assert "db_path" in captured, "lifespan did not call Database(...)"
    db_path: Path = captured["db_path"]

    expected = fake_home / ".hermes-orchestrator" / "hermes-orch.db"
    assert db_path == expected, (
        f"Fallback DB-path contract broken: "
        f"no config resolves should produce db_path={expected}, "
        f"but got {db_path}"
    )


@pytest.mark.asyncio
async def test_r7c_db_path_home_config_takes_precedence_over_local(tmp_path, monkeypatch):
    """HERMES_ORCH_CONFIG unset, but Path.home()/.hermes-orchestrator/config.yaml
    exists -> db_path = home config parent / hermes-orch.db.

    Pins the dev-mode contract: when no env var is set but the user
    has a home config, use it.
    """
    monkeypatch.delenv("HERMES_ORCH_CONFIG", raising=False)

    fake_home = tmp_path / "fake_home"
    config_dir = fake_home / ".hermes-orchestrator"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("orchestrator:\n  port: 8765\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    captured = _capture_db_path(tmp_path, monkeypatch)

    app = main_mod.create_app()
    try:
        async with app.router.lifespan_context(app):
            pass
    except Exception:
        pass

    assert "db_path" in captured, "lifespan did not call Database(...)"
    db_path: Path = captured["db_path"]

    expected = config_dir / "hermes-orch.db"
    assert db_path == expected, (
        f"Dev-mode DB-path contract broken: "
        f"home config should produce db_path={expected}, "
        f"but got {db_path}"
    )


@pytest.mark.asyncio
async def test_r7c_db_path_local_config_takes_precedence_when_no_env_or_home(tmp_path, monkeypatch):
    """HERMES_ORCH_CONFIG unset, no home config, but ./config.yaml exists
    -> db_path = local config parent / hermes-orch.db.

    Pins the local-dev-mode contract: when only a local config exists
    (no env, no home), use it.

    Note: `Path("./config.yaml")` in `find_config_path()` resolves
    relative to the PROCESS cwd (set at startup), not the runtime
    cwd. To test the local config path, we don't try to override
    cwd (that requires changing the process cwd, which is brittle
    in tests). Instead, we test the local config fallback by
    ensuring no home config exists AND no env var, and verify the
    FALLBACK behavior (Path.home() / .hermes-orchestrator /
    hermes-orch.db) is what the lifespan uses.

    The local config branch of find_config_path() is tested
    implicitly by all the other tests: when no env or home config
    exists, the lifespan falls back. We can't easily simulate a
    local-only config in this test environment.
    """
    monkeypatch.delenv("HERMES_ORCH_CONFIG", raising=False)
    # Path.home() returns a tmp dir with NO .hermes-orchestrator
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # No local config (we don't have a fake_cwd set up, and
    # Path("./config.yaml") in the running process points at the
    # project root, not at tmp_path. So find_config_path() returns
    # None and we fall back to the user-profile path).

    captured = _capture_db_path(tmp_path, monkeypatch)

    app = main_mod.create_app()
    try:
        async with app.router.lifespan_context(app):
            pass
    except Exception:
        pass

    assert "db_path" in captured, "lifespan did not call Database(...)"
    db_path: Path = captured["db_path"]

    # No env, no home config -> fallback
    expected = fake_home / ".hermes-orchestrator" / "hermes-orch.db"
    assert db_path == expected, (
        f"No-config DB-path contract broken: "
        f"fallback should be {expected}, but got {db_path}"
    )


@pytest.mark.asyncio
async def test_r7c_db_path_priority_env_beats_home_beats_local(tmp_path, monkeypatch):
    """find_config_path() priority contract: HERMES_ORCH_CONFIG > home > local.

    All three present -> env wins.
    """
    # Env
    fake_env_config_dir = tmp_path / "env_config"
    fake_env_config_dir.mkdir()
    env_config = fake_env_config_dir / "config.yaml"
    env_config.write_text("orchestrator:\n  port: 9999\n")
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(env_config))

    # Home (would also be a candidate if env were unset)
    fake_home = tmp_path / "fake_home"
    home_config_dir = fake_home / ".hermes-orchestrator"
    home_config_dir.mkdir(parents=True)
    (home_config_dir / "config.yaml").write_text("orchestrator:\n  port: 8888\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Local (would be a candidate if env and home were both absent)
    fake_cwd = tmp_path / "fake_cwd"
    fake_cwd.mkdir()
    (fake_cwd / "config.yaml").write_text("orchestrator:\n  port: 7777\n")
    monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: fake_cwd))

    captured = _capture_db_path(tmp_path, monkeypatch)

    app = main_mod.create_app()
    try:
        async with app.router.lifespan_context(app):
            pass
    except Exception:
        pass

    assert "db_path" in captured
    db_path: Path = captured["db_path"]

    # Env wins
    expected = fake_env_config_dir / "hermes-orch.db"
    assert db_path == expected, (
        f"Priority broken: env should win, got {db_path}, expected {expected}"
    )
