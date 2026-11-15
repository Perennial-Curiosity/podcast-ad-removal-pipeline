"""Small audio helpers (ffmpeg + numpy). All audio is float32 mono at SR."""
import json, re, subprocess
import numpy as np

SR = 44100


def load(path, start=None, dur=None, sr=SR):
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-i", path, "-ar", str(sr), "-ac", "1", "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def save(path, x, sr=SR, bitrate="128k"):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "-"]
    if path.endswith(".mp3"):
        cmd += ["-codec:a", "libmp3lame", "-b:a", bitrate]
    cmd += [path]
    subprocess.run(cmd, input=np.asarray(x, dtype=np.float32).tobytes(), check=True)


def loudness(path):
    """EBU R128 integrated loudness etc. via loudnorm analysis pass."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-af",
                        "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    return json.loads(m.group(0))


def loudnorm_to(path, out_wav, target_i, sr=SR):
    """Two-pass linear loudnorm of `path` to integrated loudness target_i."""
    m = loudness(path)
    tgt = max(-30.0, min(-9.0, float(target_i)))
    filt = (f"loudnorm=I={tgt:.2f}:TP=-1.0:LRA=20:linear=true:"
            f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
            f"offset={m['target_offset']}:print_format=json")
    r = subprocess.run(["ffmpeg", "-y", "-v", "info", "-i", path, "-af", filt,
                        "-ar", str(sr), "-ac", "1", out_wav], capture_output=True, text=True, check=True)
    mm = re.search(r"\{[^{}]*\"output_i\"[^{}]*\}", r.stderr, re.S)
    post = json.loads(mm.group(0)) if mm else {}
    return {"input_i": float(m["input_i"]), "target_i": tgt,
            "output_i": float(post.get("output_i", "nan")),
            "normalization_type": post.get("normalization_type")}
