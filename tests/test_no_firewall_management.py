# coding: utf-8
"""Source-grep guard: B12 hotfix must NOT introduce firewall-management
commands into executable production source (security hotfix 2026-08-11, R10).

Per `docs/security/agent-endpoint-auth-hotfix-design.md` §9.7 (revision 10):

  - Source-grep scope (allowlist — these are scanned):
      `src/hermes_orch/**` (production runtime)
      `scripts/**` (deployment / ops scripts)
  - Source-grep scope (denylist — these are NOT scanned, to prevent
    self-trigger from the test file's literal forbidden tokens and
    from design / spec docs that reference them for documentation):
      `tests/**` (this file)
      `docs/**`
      `*.md` files (anywhere)
      changelog / release notes
  - Forbidden tokens:
      `New-NetFirewallRule`, `Set-NetFirewallRule`, `Remove-NetFirewallRule`,
      `netsh advfirewall`, `iptables`, `ip6tables`, `nft`, `ufw allow`,
      or equivalent firewall-management commands.
  - Implementation strategy (preferred): use
    `git diff --name-only <base>...HEAD` to enumerate changed files;
    intersect with the executable-source allowlist; scan only those.
    Fallback: scan the allowlist tree directly.

The test is split into two parts:
  1. `test_no_firewall_management_in_full_allowlist` — scans the
     ENTIRE allowlist tree (`src/hermes_orch/**` + `scripts/**`).
     This is a regression guard for the whole repo, not just for
     this hotfix. Any new firewall-management code in any of these
     paths will fail this test.
  2. `test_no_firewall_management_in_diff_against_main` — scans
     ONLY the changed files (vs the main branch). This is the
     tightest version of the contract and is the one most directly
     relevant to the B12 hotfix. If a future PR adds firewall
     management to a different file, this test will pass for THIS
     hotfix (the B12 hotfix changes don't add it) but `test_no_firewall_management_in_full_allowlist`
     will eventually fail when CI runs against the future PR.

We run both so that:
  - The full-allowlist test catches accumulated firewall changes.
  - The diff test is precise to the current hotfix and won't trip
    on pre-existing patterns in unrelated files.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


# ===== Forbidden token list =====
# Each pattern is a literal substring OR a regex (compiled below). The
# matches are case-sensitive (firewall-management tools have a
# canonical capitalization that operators grep for, e.g.
# `New-NetFirewallRule`).
FORBIDDEN_TOKENS: list[str] = [
    # PowerShell
    "New-NetFirewallRule",
    "Set-NetFirewallRule",
    "Remove-NetFirewallRule",
    # Windows
    "netsh advfirewall",
    # Linux / iptables family
    "iptables",
    "ip6tables",
    "nft",
    # Ubuntu
    "ufw allow",
]


# Compiled regexes (case-sensitive). Each pattern is escaped to
# match the literal token; we don't want substring matches inside
# larger words (e.g. "nftables" containing "nft" is bad — we DO
# want that to fail, but we want the failure to be on the actual
# token). For a few tokens (e.g. "ufw allow"), the regex includes
# the space to avoid matching unrelated identifiers.
_COMPILED: list[tuple[str, re.Pattern[str]]] = [
    (token, re.compile(re.escape(token))) for token in FORBIDDEN_TOKENS
]


# ===== Path configuration =====
# `tests/` is the directory containing this file. `src/` and the
# repo root contain the allowlist paths.

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent

ALLOWLIST_DIRS = [
    REPO_ROOT / "src" / "hermes_orch",
    REPO_ROOT / "scripts",
]

# These extensions are considered "executable source" — text files
# that the server / scripts actually execute at runtime or deploy time.
SOURCE_EXTENSIONS = {".py", ".ps1", ".sh", ".bat", ".cmd", ".psm1"}

# Meta-scripts that LITERALLY LIST the forbidden tokens to enforce
# them. These are the documented self-trigger case: a meta-script
# that needs to KNOW the rules to enforce them MUST be excluded
# from the scan of those rules. Same logic as `tests/` excluding
# itself via the path filter in §9.7.
SELF_EXCLUDE_PATTERNS = [
    "pre_deploy_check.ps1",  # §9.3 / §9.7 self-trigger
]


def _is_source_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in SOURCE_EXTENSIONS


def _is_self_excluded(p: Path) -> bool:
    """True if this file is a meta-script that lists the rules."""
    name = p.name
    return any(pat in name for pat in SELF_EXCLUDE_PATTERNS)


def _scan_paths(paths: list[Path]) -> list[tuple[Path, str, int, str]]:
    """Scan each path (file or dir) for forbidden tokens.

    Returns a list of (path, token, line_number, line_text) for
    every match. Self-excluded meta-scripts (e.g. pre_deploy_check.ps1)
    are skipped — they LITERALLY LIST the forbidden tokens as part
    of their job, so they are the documented self-trigger case.
    """
    findings: list[tuple[Path, str, int, str]] = []
    for p in paths:
        if p.is_file():
            files = [p]
        elif p.is_dir():
            files = [f for f in p.rglob("*") if _is_source_file(f)]
        else:
            continue
        for f in files:
            if _is_self_excluded(f):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for token, pat in _COMPILED:
                    if pat.search(line):
                        findings.append((f, token, lineno, line.rstrip()))
    return findings


# ===== Test 1: full-allowlist scan =====


def test_no_firewall_management_in_full_allowlist():
    """Scans the entire allowlist tree. Catches accumulated
    firewall-management code over time."""
    findings = _scan_paths(ALLOWLIST_DIRS)
    if findings:
        # Build a human-readable diff
        lines = [
            f"Found {len(findings)} firewall-management reference(s) in allowlist:",
        ]
        for path, token, lineno, line_text in findings[:20]:
            rel = path.relative_to(REPO_ROOT) if path.is_absolute() else path
            lines.append(f"  {rel}:{lineno}: {token!r}  in: {line_text.strip()[:120]}")
        if len(findings) > 20:
            lines.append(f"  ... and {len(findings) - 20} more")
        pytest.fail("\n".join(lines))


# ===== Test 2: diff-vs-main scan =====


def test_no_firewall_management_in_diff_against_main():
    """Scans ONLY the files changed in the current branch vs main.

    The B12 hotfix should not introduce any firewall-management code
    into `src/hermes_orch/**` or `scripts/**`. If this branch added
    such code, this test fails. Pre-existing patterns in other
    files (e.g. a future unrelated PR) are NOT detected here — that
    is the job of `test_no_firewall_management_in_full_allowlist`.
    """
    # Find the merge-base with main. We use `git diff --name-only
    # main...HEAD` to enumerate changed files.
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "diff",
                "--name-only",
                "main...HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        pytest.skip(f"git not available: {e}")
    if result.returncode != 0:
        # Branch may not have a `main` reference (e.g. fresh clone
        # without origin/main). Skip rather than fail.
        pytest.skip(
            f"git diff main...HEAD failed (returncode={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    changed_files = [
        REPO_ROOT / line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    # Filter to executable-source allowlist paths only
    scan_files: list[Path] = []
    for f in changed_files:
        if not _is_source_file(f):
            continue
        try:
            rel = f.relative_to(REPO_ROOT)
        except ValueError:
            continue
        # Allowlist: must be under src/hermes_orch/ OR scripts/.
        # Denylist: skip tests/, docs/, *.md anywhere.
        rel_str = str(rel).replace("\\", "/")
        if rel_str.startswith("tests/"):
            continue
        if rel_str.startswith("docs/"):
            continue
        if rel.suffix.lower() == ".md":
            continue
        if rel_str.startswith("src/hermes_orch/") or rel_str.startswith("scripts/"):
            scan_files.append(f)
    findings = _scan_paths(scan_files)
    if findings:
        lines = [
            f"Found {len(findings)} firewall-management reference(s) "
            f"in B12 hotfix diff (vs main):",
        ]
        for path, token, lineno, line_text in findings:
            rel = path.relative_to(REPO_ROOT) if path.is_absolute() else path
            lines.append(
                f"  {rel}:{lineno}: {token!r}  in: {line_text.strip()[:120]}"
            )
        pytest.fail("\n".join(lines))


# ===== Sanity check: the test scanner itself works =====


def test_scanner_detects_forbidden_tokens_in_known_string():
    """Sanity: the scanner's regex set actually matches the forbidden
    tokens. If the regex was wrong, the no-firewall test would
    silently pass on bad code."""
    sample = (
        "New-NetFirewallRule -DisplayName 'open 8765' -Direction Inbound\n"
        "iptables -A INPUT -p tcp --dport 8765 -j ACCEPT\n"
        "ufw allow 8765\n"
    )
    findings = _scan_paths_for_text(sample)
    tokens_found = {token for _, token, _, _ in findings}
    assert "New-NetFirewallRule" in tokens_found
    assert "iptables" in tokens_found
    assert "ufw allow" in tokens_found


def _scan_paths_for_text(text: str) -> list[tuple[Path, str, int, str]]:
    """Helper for the sanity test: scan a single string for forbidden
    tokens. Returns a list mimicking `_scan_paths` shape but with a
    fake path."""
    findings: list[tuple[Path, str, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for token, pat in _COMPILED:
            if pat.search(line):
                findings.append((Path("<sample>"), token, lineno, line))
    return findings
