"""Extract <script> blocks from visual_project.html and check JS syntax."""
import pathlib, re, sys, subprocess, tempfile, os

p = pathlib.Path("src/hermes_orch/templates/visual_project.html")
html = p.read_text(encoding="utf-8")
# Match inline <script> blocks (id may or may not be present)
# We want all scripts EXCEPT the <script id="vp-task-data" type="application/json">
# which is a JSON data island, not executable JS.
pattern = re.compile(r"<script(?P<attrs>(?![^>]*type=\"application/json\")[^>])*>(?P<code>.*?)</script>", re.S)
matches = list(pattern.finditer(html))
print(f"Found {len(matches)} executable <script> blocks")

# Strip Jinja2 template placeholders ({{ ... }}) so node --check can
# parse the file. We replace with safe JS equivalents:
#   {{ x | tojson }}       -> null   (literal)
#   {{ x }}                 -> null
#   {% ... %}              -> empty
#   {# ... #}              -> empty
def _strip_jinja(s):
    s = re.sub(r"\{#.*?#\}", "", s, flags=re.S)         # comments
    s = re.sub(r"\{%.*?%\}", "", s, flags=re.S)         # blocks
    s = re.sub(r"\{\{.*?\}\}", "null", s, flags=re.S)   # expressions
    return s

all_ok = True
for i, m in enumerate(matches, 1):
    code = m.group("code")
    if not code.strip():
        continue
    code_check = _strip_jinja(code)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(code_check)
        fn = f.name
    try:
        r = subprocess.run(
            ["node", "--check", fn],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            print(f"  block #{i} ({len(code)} chars): OK")
        else:
            all_ok = False
            print(f"  block #{i} ({len(code)} chars): FAIL")
            print("    stderr:", r.stderr.strip())
    finally:
        os.unlink(fn)

sys.exit(0 if all_ok else 1)
