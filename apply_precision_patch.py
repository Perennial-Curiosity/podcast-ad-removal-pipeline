#!/usr/bin/env python3
"""Rebuild and apply precision-improvements-heatmap-v1.patch (Grok)."""
import base64
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
upload = root / "upload"
parts = sorted(upload.glob("patch.p*.b64"))
if not parts:
    sys.exit(f"no parts in {upload}/patch.p*.b64")
raw = base64.b64decode("".join(p.read_text().strip() for p in parts))
patch_path = root / "precision-improvements-heatmap-v1.patch"
patch_path.write_bytes(raw)
print(f"wrote {patch_path} ({len(raw)} bytes)")

r = subprocess.run(
    ["patch", "-p1", "--dry-run", "-i", str(patch_path)],
    cwd=root, capture_output=True, text=True,
)
if r.returncode != 0:
    print(r.stdout)
    print(r.stderr)
    sys.exit("patch --dry-run failed")
subprocess.check_call(["patch", "-p1", "-i", str(patch_path)], cwd=root)
print("applied. Next: git add pipeline/run_pipeline.py && git commit -m 'Apply Grok precision heat-map patch' && git push")
