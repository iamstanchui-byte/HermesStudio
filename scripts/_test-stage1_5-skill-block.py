"""Stage 1.5 wrapper-level tests: verify the SKILL block is actually
rendered and inserted into the task prompt when _workflow_skill is
present in task params.

Stage 1.5 (2026-07-23): workflow step -> skill reference ->
run endpoint puts _workflow_skill in task params ->
wrapper reads it, fetches SKILL.md, injects as --- SKILL: <name> --- block.

This test calls _render_workflow_skill_block directly (unit test).
For an E2E that proves the full prompt pipeline, see the live
hermes.*.stdout.log after running a workflow with skill.
"""
import os
import sys
import tempfile
from pathlib import Path

# Repo root on path so we can import the wrapper module
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from hermes_orch.agent_cli import _render_workflow_skill_block


def test(name, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}{('  ' + detail) if detail else ''}")
    if not ok:
        raise AssertionError(f"{name}: {detail}")


def main():
    print("[1] _render_workflow_skill_block")
    with tempfile.TemporaryDirectory() as tmp:
        profile_root = Path(tmp)
        # No skill yet - returns "" + warns
        out = _render_workflow_skill_block(profile_root, "nope")
        test("missing skill returns ''", out == "", f"got {out!r}")

        # Create a skill
        skill_dir = profile_root / "skills" / "bus" / ""
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            "---\nname: bus\n---\n\n# Bus lookup procedure\n\n"
            "Use https://search.kmb.hk to find bus routes.\n"
            "Completion criteria: return a list of bus numbers.\n",
            encoding="utf-8",
        )

        out = _render_workflow_skill_block(profile_root, "bus")
        test("returns non-empty block", len(out) > 0)
        test("block starts with '--- SKILL: bus'", out.startswith("--- SKILL: bus (workflow reference, body prepended) ---"))
        test("block contains the skill body", "https://search.kmb.hk" in out)
        test("block has END SKILL marker", "--- END SKILL: bus ---" in out)
        test("block has END SKILL HINT marker", "--- END SKILL HINT ---" in out)
        test("block has the workflow-reference hint", "referenced by a workflow step" in out)

    # Truncation test
    print()
    print("[2] 100KB truncation")
    with tempfile.TemporaryDirectory() as tmp:
        profile_root = Path(tmp)
        skill_dir = profile_root / "skills" / "huge"
        skill_dir.mkdir(parents=True, exist_ok=True)
        big = "x" * (150_000)  # 150KB of body
        (skill_dir / "SKILL.md").write_text(big, encoding="utf-8")
        out = _render_workflow_skill_block(profile_root, "huge")
        test("truncated output is bounded", len(out) < 105_000, f"got {len(out)}")
        test("contains truncation marker", "truncated to 100KB" in out)

    # Empty skill_name returns ""
    print()
    print("[3] edge cases")
    out = _render_workflow_skill_block(Path("/tmp"), "")
    test("empty skill_name returns ''", out == "")

    # _strip_prompt_echo must remove the SKILL block echo too
    print()
    print("[4] _strip_prompt_echo with SKILL block")
    from hermes_orch.agent_cli import _strip_prompt_echo
    sample = (
        "Query: investigate_topic({'topic': 'bus 6'})\n\n"
        "--- OUTPUT FORMAT ---\nOutput file conventions...\n--- END OUTPUT FORMAT ---\n\n"
        "--- SKILL: bus (workflow reference, body prepended) ---\n# Bus skill\n--- END SKILL: bus ---\n\n"
        "This skill was referenced by a workflow step.\n--- END SKILL HINT ---\n\n"
        "Here is the actual answer: bus 6 runs from Yuen Long to Mong Kok.\n"
    )
    stripped = _strip_prompt_echo(sample)
    test("prompt echo stripped (no Query:)", not stripped.startswith("Query:"))
    test("prompt echo stripped (no OUTPUT FORMAT marker)", "--- END OUTPUT FORMAT ---" not in stripped[:200])
    test("prompt echo stripped (no SKILL marker)", "--- END SKILL: bus ---" not in stripped[:200])
    test("answer preserved", "bus 6 runs from Yuen Long" in stripped)

    print()
    print("=== ALL PASS ===")


if __name__ == "__main__":
    main()
