"""Split a writer's out.json into the card files and run the SEO checker.

  ./venv13/bin/python3 scratch/seo/lean/check.py scratch/seo/lean/<model>/<code>
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
d = Path(sys.argv[1])
try:
    out = json.loads((d / "out.json").read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    print(f"ERROR out.json не читається: {exc}\nRESULT: 1 ERROR")
    sys.exit(1)
(d / "core.json").write_text(json.dumps(out.get("core", {}), ensure_ascii=False, indent=2), encoding="utf-8")
(d / "gz.json").write_text(json.dumps(out.get("gz", {}), ensure_ascii=False, indent=2), encoding="utf-8")
(d / "draft.md").write_text(out.get("draft", ""), encoding="utf-8")
tech = out.get("technical", [])
if tech == "ok" and (d / "pack_technical.json").exists():
    tech = json.loads((d / "pack_technical.json").read_text(encoding="utf-8"))
out["technical"] = tech
(d / "technical.json").write_text(json.dumps(tech, ensure_ascii=False, indent=2), encoding="utf-8")
res = subprocess.run([sys.executable, str(ROOT / "tools/seo_text_check.py"),
                      "--draft", str(d / "draft.md"), "--core", str(d / "core.json")], capture_output=True, text=True)
lines = [l for l in res.stdout.splitlines() if l.startswith(("ERROR", "RESULT", "ГЗ:"))]
gz = out.get("gz", {})
missing = [k for k in ("gz", "h1", "h1_ru", "title", "title_ru", "description", "description_ru") if not gz.get(k)]
if missing:
    lines.insert(-1, f"ERROR [gz] немає полів: {', '.join(missing)}")
if not out.get("technical"):
    lines.insert(-1, "ERROR [technical] порожньо")
print("\n".join(lines))
# Attempt counter for the experiment report.
cnt = d / ".attempts"
cnt.write_text(str(int(cnt.read_text() or 0) + 1) if cnt.exists() else "1")
sys.exit(0 if res.returncode == 0 and not missing and out.get("technical") else 1)
