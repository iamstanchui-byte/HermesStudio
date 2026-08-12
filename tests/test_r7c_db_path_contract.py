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
`config_path = find_config_path(); db_path = config_path.parent / "hermes-orch.db"`.

The 2026-08-11 review tightened this further to a "single resolution"
contract: `find_config_path()` is called exactly once, and the SAME
resolved path is passed to both `load_config()` and DB derivation.
This file pins the contract:

  1. With HERMES_ORCH_CONFIG set to a ProgramData path, db_path is
     the config's parent dir + "hermes-orch.db".
  2. With NO config resolvable (env unset, no home config, no local
     config), db_path falls back to
     `Path.home() / ".hermes-orchestrator" / "hermes-orch.db"`
     (the historical user-profile path).
  3. With local `./config.yaml` (no env, no home), db_path is the
     local config's parent dir + "hermes-orch.db".
  4. Under LocalSystem (Path.home() == systemprofile), with
     HERMES_ORCH_CONFIG set, the db_path does NOT include
     "systemprofile" -- the production setup takes precedence.
  5. When HERMES_ORCH_CONFIG is unset but the home dir HAS a
     config.yaml, db_path is the home config's parent dir +
     "hermes-orch.db" (dev mode).
  6. `find_config_path()` is called EXACTLY ONCE in the lifespan.
     Double-resolution would let env / cwd drift between config
     load and DB derivation.
  7. `load_config()` receives the SAME resolved config path
     (the DB's parent dir equals the config file's parent dir).

These tests use a `StopAfterDbPathCapture` sentinel raised by the
mocked `Database.connect()` so the test only accepts the expected
stop signal and FAILS on any other startup error (config
migration, missing import, etc.) -- not silently swallow them.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_orch import config as config_mod
from hermes_orch import main as main_mod
from hermes_orch import db as db_mod


# ===== Sentinel + helpers =====


class StopAfterDbPathCapture(Exception):
    """Sentinel raised by the mocked `Database.connect()` once
    `Database.__init__` has captured the db_path. The test catches
    ONLY this exception -- any other startup error (config migration,
    import, etc.) is a real failure and propagates as a test FAIL.
    """


def _capture_db_path(monkeypatch):
    """Patch `Database.__init__` (capture db_path) + `Database.connect`
    (raise sentinel) so the test can verify db_path without opening
    a real DB.

    Returns a dict that will be populated with `"db_path"` once the
    lifespan calls `Database(db_path)`. The lifespan then calls
    `await db.connect()`, which raises `StopAfterDbPathCapture`,
    cleanly stopping startup AFTER db_path has been captured.
    """
    captured: dict[str, Path] = {}

    def patched_init(self, db_path):
        captured["db_path"] = Path(db_path)
        # Don't open the real DB; just store the path so the
        # constructor doesn't fail if anything reads it.
        self.db_path = str(db_path)

    async def patched_connect(self):
        # Abort startup AFTER db_path was captured. This is the
        # ONLY way the test "passes" -- any other exception
        # propagates and fails the test.
        raise StopAfterDbPathCapture(captured.get("db_path"))

    monkeypatch.setattr(db_mod.Database, "__init__", patched_init)
    monkeypatch.setattr(db_mod.Database, "connect", patched_connect)
    return captured


async def _run_lifespan_and_capture_db_path_async(monkeypatch):
    """Run the lifespan under the db-path-capture patches and
    return the captured db_path. Fails the test on any exception
    other than `StopAfterDbPathCapture`.

    Async because the lifespan is async; callers must be
    `async def` themselves (decorated with
    `@pytest.mark.asyncio`).
    """
    captured = _capture_db_path(monkeypatch)
    app = main_mod.create_app()
    try:
        async with app.router.lifespan_context(app):
            pass  # capture happens in patched_init + patched_connect
    except StopAfterDbPathCapture:
        # Expected: lifespan stopped after db_path was captured.
        pass
    assert "db_path" in captured, (
        "lifespan did not call Database(...) -- R7-C contract "
        "is not being exercised."
    )
    return captured["db_path"]


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

    db_path = await _run_lifespan_and_capture_db_path_async(monkeypatch)

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
    returns None, we use the historical user-profile path.
    """
    # No HERMES_ORCH_CONFIG
    monkeypatch.delenv("HERMES_ORCH_CONFIG", raising=False)

    # No home config (override Path.home to a tmp dir with no
    # .hermes-orchestrator/config.yaml).
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # No local config: chdir to a tmp dir that has no config.yaml.
    # find_config_path() checks Path("./config.yaml") against the
    # process cwd, so monkeypatch.chdir is the standard way to
    # control this.
    fake_cwd = tmp_path / "fake_cwd"
    fake_cwd.mkdir()
    monkeypatch.chdir(fake_cwd)

    db_path = await _run_lifespan_and_capture_db_path_async(monkeypatch)

    expected = fake_home / ".hermes-orchestrator" / "hermes-orch.db"
    assert db_path == expected, (
        f"Fallback DB-path contract broken: "
        f"no config resolves should produce db_path={expected}, "
        f"but got {db_path}"
    )


@pytest.mark.asyncio
async def test_r7c_db_path_local_config_takes_precedence_when_no_env_or_home(
    tmp_path, monkeypatch
):
    """HERMES_ORCH_CONFIG unset, no home config, but ./config.yaml exists
    -> db_path = local config parent / hermes-orch.db.

    Pins the local-dev-mode contract: when only a local config
    exists (no env, no home), use it. This is the real
    "local config" test the 2026-08-11 review demanded -- it
    actually creates a `./config.yaml` in the cwd and verifies
    the lifespan uses it.
    """
    # No env
    monkeypatch.delenv("HERMES_ORCH_CONFIG", raising=False)

    # No home config (Path.home() -> tmp dir with no .hermes-orchestrator)
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Local config EXISTS: chdir to a tmp dir that HAS config.yaml
    fake_cwd = tmp_path / "fake_cwd"
    fake_cwd.mkdir()
    local_config = fake_cwd / "config.yaml"
    local_config.write_text("orchestrator:\n  port: 8765\n")
    monkeypatch.chdir(fake_cwd)

    db_path = await _run_lifespan_and_capture_db_path_async(monkeypatch)

    # Deterministic contract: db_path is the local config's
    # parent dir + hermes-orch.db (NOT the home fallback).
    expected = fake_cwd / "hermes-orch.db"
    assert db_path == expected, (
        f"Local-config DB-path contract broken: "
        f"local ./config.yaml should produce db_path={expected}, "
        f"but got {db_path}"
    )
    # Regression guard: db_path must NOT be the home fallback.
    assert db_path != fake_home / ".hermes-orchestrator" / "hermes-orch.db", (
        f"DB-path contract REGRESSION: local config was ignored; "
        f"db_path={db_path} fell back to home. The 2026-08-11 "
        f"review required this test to actually exercise the "
        f"local-config branch."
    )


@pytest.mark.asyncio
async def test_r7c_db_path_home_config_takes_precedence_over_local(
    tmp_path, monkeypatch
):
    """HERMES_ORCH_CONFIG unset, but Path.home()/.hermes-orchestrator/config.yaml
    exists -> db_path = home config parent / hermes-orch.db.

    Pins the dev-mode contract: when no env var is set but the user
    has a home config, use it. Local ./config.yaml is IGNORED
    (env > home > local priority).
    """
    monkeypatch.delenv("HERMES_ORCH_CONFIG", raising=False)

    fake_home = tmp_path / "fake_home"
    config_dir = fake_home / ".hermes-orchestrator"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("orchestrator:\n  port: 8765\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Even if a local config exists, home wins.
    fake_cwd = tmp_path / "fake_cwd"
    fake_cwd.mkdir()
    (fake_cwd / "config.yaml").write_text("orchestrator:\n  port: 9999\n")
    monkeypatch.chdir(fake_cwd)

    db_path = await _run_lifespan_and_capture_db_path_async(monkeypatch)

    expected = config_dir / "hermes-orch.db"
    assert db_path == expected, (
        f"Dev-mode DB-path contract broken: "
        f"home config should produce db_path={expected}, "
        f"but got {db_path}"
    )


@pytest.mark.asyncio
async def test_r7c_db_path_priority_env_beats_home_beats_local(
    tmp_path, monkeypatch
):
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
    monkeypatch.chdir(fake_cwd)

    db_path = await _run_lifespan_and_capture_db_path_async(monkeypatch)

    # Env wins
    expected = fake_env_config_dir / "hermes-orch.db"
    assert db_path == expected, (
        f"Priority broken: env should win, got {db_path}, expected {expected}"
    )


@pytest.mark.asyncio
async def test_r7c_db_path_single_resolution(monkeypatch, tmp_path):
    """`find_config_path()` is called EXACTLY ONCE in the lifespan.

    The 2026-08-11 review demanded this: the DB path must be derived
    from the SAME config path that `load_config()` used. If the
    implementation calls `find_config_path()` twice, an env / cwd /
    file-existence change between calls could let config A be loaded
    while config B's parent dir determines the DB path.

    Test: monkeypatch `find_config_path` to return a known path on
    the first call and RAISE on any subsequent call. If the
    implementation calls it twice, the second call propagates a
    RuntimeError and the test fails. The captured db_path must
    also equal the first-call resolution.
    """
    # Env-controlled config (the path we'll return on 1st call)
    env_config_dir = tmp_path / "env_only"
    env_config_dir.mkdir()
    env_config = env_config_dir / "config.yaml"
    env_config.write_text("orchestrator:\n  port: 8765\n")
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(env_config))

    # Other paths: NOT present, so the only resolution is the env one.
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    fake_cwd = tmp_path / "fake_cwd"
    fake_cwd.mkdir()
    monkeypatch.chdir(fake_cwd)

    # Patch BOTH `config_mod.find_config_path` and `main_mod.find_config_path`
    # because `main.py` did `from hermes_orch.config import find_config_path`,
    # which is a separate name binding in the `main` module's namespace.
    call_count = [0]

    def counting_find_config_path():
        call_count[0] += 1
        if call_count[0] == 1:
            return env_config
        # 2nd+ call = double resolution. R7-C contract broken.
        raise RuntimeError(
            f"find_config_path() called {call_count[0]} times; "
            f"R7-C contract requires EXACTLY 1 call. "
            f"The DB path and the loaded config must come from "
            f"the SAME resolved config path."
        )

    monkeypatch.setattr(config_mod, "find_config_path", counting_find_config_path)
    monkeypatch.setattr(main_mod, "find_config_path", counting_find_config_path)

    # The lifespan calls load_config() FIRST (which calls
    # find_config_path() internally -- the ONE allowed call), then
    # derives db_path from the resolved path. If main.py
    # re-resolves by calling find_config_path() AGAIN, this test
    # fails with RuntimeError.
    db_path = await _run_lifespan_and_capture_db_path_async(monkeypatch)

    # EXACTLY 1 call
    assert call_count[0] == 1, (
        f"find_config_path() was called {call_count[0]} times; "
        f"R7-C contract requires EXACTLY 1. Double-resolution is "
        f"the regression the 2026-08-11 review demanded we fix."
    )
    # db_path is derived from the first-call resolution
    expected = env_config.parent / "hermes-orch.db"
    assert db_path == expected, (
        f"DB path should come from the single resolution, "
        f"got {db_path}, expected {expected}"
    )


@pytest.mark.asyncio
async def test_r7c_db_path_load_config_uses_same_path(tmp_path, monkeypatch):
    """`load_config()` receives the SAME resolved config path that
    drives the DB derivation. The two must come from the same
    `find_config_path()` call.

    Test: use a config file with a distinctive value (port: 5555).
    The lifespan calls `load_config(config_path=...)` with the
    resolved path. We can verify load_config saw the right file
    by checking the loaded cfg's orchestrator.port. (Indirect
    but observable: the test framework exposes `app.state.config`
    via the `StopAfterDbPathCapture` capture point -- but we
    don't have that here. Instead, the single-resolution test
    already pins the contract; this test pins the FILE LOADED
    side via the same channel.)
    """
    env_config_dir = tmp_path / "distinct"
    env_config_dir.mkdir()
    env_config = env_config_dir / "config.yaml"
    # Distinctive port so we can verify the right file was loaded
    env_config.write_text("orchestrator:\n  port: 5555\n  log_level: DEBUG\n")
    monkeypatch.setenv("HERMES_ORCH_CONFIG", str(env_config))

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    fake_cwd = tmp_path / "fake_cwd"
    fake_cwd.mkdir()
    monkeypatch.chdir(fake_cwd)

    # Spy on load_config to capture what config_path it received.
    load_config_calls: list[Path | None] = []
    real_load_config = config_mod.load_config

    def spy_load_config(config_path=None):
        load_config_calls.append(config_path)
        return real_load_config(config_path=config_path)

    monkeypatch.setattr(config_mod, "load_config", spy_load_config)
    monkeypatch.setattr(main_mod, "load_config", spy_load_config)

    db_path = await _run_lifespan_and_capture_db_path_async(monkeypatch)

    # load_config was called exactly once
    assert len(load_config_calls) == 1, (
        f"load_config() called {len(load_config_calls)} times; "
        f"lifespan should call it exactly once."
    )
    # load_config received the same env path we expect
    assert load_config_calls[0] == env_config, (
        f"load_config received {load_config_calls[0]!r}, "
        f"expected {env_config!r}. DB and config must use the "
        f"SAME resolved path."
    )
    # db_path derives from that same config
    assert db_path == env_config_dir / "hermes-orch.db"
