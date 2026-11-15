"""Read-only import of the pipeline under test (default: this worktree's
committed copy of pipeline/run_pipeline.py from grok/boundary-work)."""
import importlib.util, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT = os.path.join(REPO, "pipeline", "run_pipeline.py")


def load_pipeline(path=None):
    path = path or os.environ.get("PAD_PIPELINE", DEFAULT)
    os.environ.setdefault("PODCAST_ADS_DIR", os.path.join(REPO, ".pad_base"))
    spec = importlib.util.spec_from_file_location("run_pipeline", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_pipeline"] = mod
    sys.path.insert(0, os.path.dirname(path))
    spec.loader.exec_module(mod)
    os.environ.pop("HF_HUB_OFFLINE", None)
    mod._pad_source = path
    try:
        mod._pad_commit = subprocess.run(["git", "-C", os.path.dirname(path), "log", "-1",
                                          "--format=%h", "--", path],
                                         capture_output=True, text=True).stdout.strip()
    except Exception:
        mod._pad_commit = "?"
    return mod
