"""Inject the pipeline's JSON bundle into the dashboard template -> output/dashboard.html"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
tpl = (ROOT / "src" / "dashboard_template.html").read_text()
data = (ROOT / "output" / "dashboard_data.json").read_text()
html = tpl.replace("/*__DATA__*/null", data)
(ROOT / "output" / "dashboard.html").write_text(html)
print(f"dashboard.html written ({len(html) / 1e6:.2f} MB)")
