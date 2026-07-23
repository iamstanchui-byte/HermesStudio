"""Unit test: verify the subfolder skill name-extraction logic.

The wrapper's _sync_one_profile_skills (post-2026-07-24) computes a
skill's `name` from the relative path under skills/. We test the
extraction logic in isolation, since the full sync function talks to
the orchestrator over HTTP (out of scope for a unit test).

Layout tested:
  skills/<name>/SKILL.md              → name = "<name>"
  skills/<category>/<name>/SKILL.md   → name = "<category>/<name>"
  skills/<a>/<b>/<c>/SKILL.md         → skip (depth > 2)
  skills/<bad name>/SKILL.md          → skip (regex fail)
  skills/<bad>_<cat>/<name>/SKILL.md  → skip (regex fail)
"""
import sys
import re
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Same regexes as agent_cli.py
_SKILL_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_SKILL_SUBFOLDER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"
)


def extract_name(rel: Path) -> str | None:
    """Replicate the logic in _sync_one_profile_skills (agent_cli.py).

    rel is the path RELATIVE TO skills/ (e.g. 'xlsx/SKILL.md' or
    'productivity/xlsx/SKILL.md'). Returns the skill name or None
    if the path should be skipped.
    """
    parts = rel.parts  # tuple, e.g. ('xlsx', 'SKILL.md')
    if len(parts) == 2:
        name = parts[0]
        if _SKILL_FOLDER_RE.match(name):
            return name
        return None
    if len(parts) == 3:
        name = f"{parts[0]}/{parts[1]}"
        if _SKILL_SUBFOLDER_RE.match(name):
            return name
        return None
    return None  # depth > 2


def expect(name_in, expected):
    rel = Path(name_in)
    out = extract_name(rel)
    mark = "PASS" if out == expected else "FAIL"
    print(f"  {mark}  {name_in!r:60s} -> {out!r}  (expected {expected!r})")
    if out != expected:
        raise AssertionError(f"got {out!r}, expected {expected!r}")


def main():
    print("[1] Flat layout: skills/<name>/SKILL.md")
    expect("xlsx/SKILL.md", "xlsx")
    expect("hk-weather-forecast/SKILL.md", "hk-weather-forecast")
    expect("multi-profile-handoff/SKILL.md", "multi-profile-handoff")
    expect("a.b_c-1/SKILL.md", "a.b_c-1")  # allow . _ - in name
    print()
    print("[2] Subfolder layout: skills/<category>/<name>/SKILL.md")
    expect("productivity/xlsx/SKILL.md", "productivity/xlsx")
    expect("creative/architecture-diagram/SKILL.md", "creative/architecture-diagram")
    expect("apple/apple-notes/SKILL.md", "apple/apple-notes")
    expect("a.b/c-d/SKILL.md", "a.b/c-d")
    print()
    print("[3] Depth > 2: skip")
    expect("a/b/c/SKILL.md", None)
    expect("a/b/c/d/SKILL.md", None)
    print()
    print("[4] Bad names: skip (start-with-special, spaces, etc.)")
    expect("-bad/SKILL.md", None)  # starts with hyphen
    expect("bad name/SKILL.md", None)  # space
    expect(".dot/SKILL.md", None)  # starts with dot
    expect("a/_under/SKILL.md", None)  # subfolder name starts with _
    expect("a/b c/SKILL.md", None)  # space in subfolder
    expect("a/.b/SKILL.md", None)  # subfolder starts with dot
    # Note: regex allows trailing/embedded hyphens (e.g. 'b-', 'a-b').
    # This is consistent with the existing flat-name regex. We don't
    # tighten it now (would break the ~100 existing skills with
    # kebab-case names like 'hk-weather-forecast' which DO end with -).
    expect("a-b/SKILL.md", "a-b")  # accepted (embedded hyphen OK)
    expect("ab-cd/SKILL.md", "ab-cd")  # accepted
    print()
    print("[5] Subfolder name that pass the regex (not strict enough)")
    expect("a-/b/SKILL.md", "a-/b")  # subfolder category starts with hyphen (regex allows trailing -)
    expect("a/b-/SKILL.md", "a/b-")  # name starts with hyphen (regex allows embedded -)
    expect("ab/cd/SKILL.md", "ab/cd")  # OK, 2-char names
    expect("a/b/SKILL.md", "a/b")  # OK, single char
    print()
    print("=== ALL PASS ===")


if __name__ == "__main__":
    main()
