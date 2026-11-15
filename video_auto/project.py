"""app.py가 저장한 projects/<이름>/ 폴더 읽기 및 공용 유틸."""
import json
import re
from pathlib import Path

DEFAULT_SILENT = ("No dialogue, no speech, no voiceover, no characters talking or lip-syncing, "
                  "no background music, no sound effects. Silent video.")
FALLBACK_MOTION = "Slow gentle camera push-in, subtle ambient movement, keep the illustration style unchanged."

# 화면 비율 → 720p 기준 (가로, 세로)
BASE_SIZES = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720), "4:3": (960, 720), "3:4": (720, 960)}


def frame_size(aspect: str, height: int = 720):
    w, h = BASE_SIZES.get(aspect, BASE_SIZES["16:9"])
    k = height / 720
    even = lambda v: int(round(v * k / 2)) * 2  # noqa: E731
    return even(w), even(h)


def ensure_silent(text: str, directive: str) -> str:
    text, directive = (text or "").strip(), (directive or "").strip()
    if not text or not directive or directive.lower() in text.lower():
        return text
    return f"{text} {directive}"


def load_project(project_dir):
    d = Path(project_dir)
    pj = d / "project.json"
    if not pj.exists():
        raise FileNotFoundError(f"{pj} 가 없습니다. 앱에서 프로젝트를 먼저 저장하세요.")
    data = json.loads(pj.read_text(encoding="utf-8"))
    inputs = data.get("inputs") or {}
    silent = inputs.get("silent_rule") or DEFAULT_SILENT
    scenes = []
    for s in data.get("scenes", []):
        imgs = sorted((d / "images").glob(f'{s["idx"]:03d}.*'))
        scenes.append({
            "idx": int(s["idx"]), "start": float(s["start"]), "end": float(s["end"]),
            "text": s.get("text", ""), "image": imgs[0] if imgs else None,
            "motion": ensure_silent(s.get("motion") or FALLBACK_MOTION, silent),
        })
    if not scenes:
        raise ValueError("프로젝트에 장면이 없습니다.")
    return {"dir": d, "inputs": inputs, "scenes": scenes, "aspect": inputs.get("aspect") or "16:9"}


def parse_ai_spec(spec: str, idxs):
    """AI 영상으로 만들 장면 고르기.
    'all' / 'none' / '30%'(첫 장면 포함 고르게) / '1,3,5-8'(번호)"""
    spec = (spec or "").strip().lower()
    idxs = list(idxs)
    if spec in ("", "none", "0", "0%"):
        return set()
    if spec in ("all", "100%"):
        return set(idxs)
    if spec.endswith("%"):
        n = max(1, round(len(idxs) * float(spec[:-1]) / 100))
        n = min(n, len(idxs))
        return {idxs[int(i * len(idxs) / n)] for i in range(n)}
    out = set()
    for part in re.split(r"[,\s]+", spec):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out & set(idxs)


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(scenes, path):
    out = [f'{i}\n{srt_time(s["start"])} --> {srt_time(s["end"])}\n{s["text"]}\n'
           for i, s in enumerate(scenes, 1) if s["text"].strip()]
    Path(path).write_text("\n".join(out), encoding="utf-8")
