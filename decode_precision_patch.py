#!/usr/bin/env python3
"""Assemble precision-improvements-heatmap-v1.patch from base64 parts."""
import base64
from pathlib import Path

parts = [
    Path("precision-improvements-heatmap-v1.patch.b64.1").read_text().strip(),
    Path("precision-improvements-heatmap-v1.patch.b64.2").read_text().strip(),
]
Path("precision-improvements-heatmap-v1.patch").write_bytes(
    base64.b64decode("".join(parts))
)
print("wrote precision-improvements-heatmap-v1.patch")
print("next: git apply precision-improvements-heatmap-v1.patch")
