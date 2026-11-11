#!/usr/bin/env python3
"""Assemble pipeline/run_pipeline.py from upload/run_pipeline.part-XX.b64 (Grok)."""
import base64, ast
from pathlib import Path
root = Path(__file__).resolve().parent
upload = root / "upload"
n = 80
chunks = []
for i in range(n):
    p = upload / f"run_pipeline.part-{i:02d}.b64"
    if not p.exists():
        raise SystemExit(f"missing {p}")
    chunks.append(p.read_text().strip())
data = base64.b64decode("".join(chunks))
target = root / "pipeline" / "run_pipeline.py"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_bytes(data)
print(f"wrote {target} ({len(data)} bytes)")
ast.parse(data.decode("utf-8"))
print("syntax OK")
