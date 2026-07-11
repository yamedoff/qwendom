#!/usr/bin/env python3
from pathlib import Path

root = Path("/data/q4")
dist = Path("/data/dist")
dist.mkdir(exist_ok=True)

css = (root / "core.css").read_text()
js = (root / "core.js").read_text()

for src in sorted((root / "src").glob("*.html")):
    html = src.read_text()
    assert "<!--CORE_CSS-->" in html and "<!--CORE_JS-->" in html, src.name
    html = html.replace("<!--CORE_CSS-->", css).replace("<!--CORE_JS-->", js)
    (dist / src.name).write_text(html)
    print(f"built {src.name}: {len(html)} bytes")
