# run_pipeline.py reconstruction parts

These part_*.txt files concatenate in order (000..029) to reconstitute the full improved `pipeline/run_pipeline.py` from local commit 79ad5b7 (authored by Grok).

```bash
cat upload/run_pipeline.parts/part_*.txt > pipeline/run_pipeline.py
```

Or with Python:
```python
from pathlib import Path
p = Path('upload/run_pipeline.parts')
Path('pipeline/run_pipeline.py').write_text(''.join(f.read_text() for f in sorted(p.glob('part_*.txt'))))
```
