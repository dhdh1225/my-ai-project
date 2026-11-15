"""ffmpeg으로 줌·패닝 클립 만들기, AI 클립 길이 맞추기, 이어 붙이기, 오디오 합성."""
import math
import shutil
import subprocess
import tempfile
from pathlib import Path

ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
       "-video_track_timescale", "15360", "-an"]


def check_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg이 설치돼 있지 않습니다. (윈도우: winget install ffmpeg / 맥: brew install ffmpeg)")


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 오류: {' '.join(map(str, cmd[:6]))} …\n{p.stderr[-1500:]}")
    return p


def duration(path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(p.stdout.strip() or 0)


def _zoom_expr(variant: int, n: int, s: float):
    N = max(n - 1, 1)
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return [
        (f"1+{s}*on/{N}", cx, cy),                                   # 천천히 확대
        (f"{1 + s}", f"(iw-iw/zoom)*on/{N}", "(ih-ih/zoom)/2"),       # 왼쪽 → 오른쪽
        (f"{1 + s}-{s}*on/{N}", cx, cy),                             # 천천히 축소
        (f"{1 + s}", f"(iw-iw/zoom)*(1-on/{N})", "(ih-ih/zoom)/2"),   # 오른쪽 → 왼쪽
    ][variant % 4]


def kenburns(image, out, frames: int, w: int, h: int, fps: int, variant: int = 0, strength: float = 0.12):
    """정지 이미지에 부드러운 확대/이동 효과. 떨림을 줄이려고 크게 키운 뒤 zoompan."""
    f = max(2, round(5000 / max(w, h)))
    z, x, y = _zoom_expr(variant, frames, strength)
    vf = (f"scale={w * f}:{h * f}:force_original_aspect_ratio=increase:flags=lanczos,crop={w * f}:{h * f},"
          f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={fps},setsar=1")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(image), "-vf", vf, "-frames:v", str(frames),
         "-r", str(fps), *ENC, str(out)])


def concat(files, out):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as t:
        for f in files:
            t.write(f"file '{Path(f).resolve().as_posix()}'\n")
        lst = t.name
    try:
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", str(out)])
    finally:
        Path(lst).unlink(missing_ok=True)


def fit_clip(raw, out, frames: int, w: int, h: int, fps: int, max_slow: float = 1.5, variant: int = 0):
    """AI 클립을 장면 길이(frames)에 맞춘다.
    길면 자르고, 조금 짧으면 천천히 재생(최대 max_slow배), 많이 짧으면 마지막 장면에 줌효과를 이어 붙인다."""
    dur = frames / fps
    L = duration(raw)
    base = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps={fps}"
    pad = "tpad=stop_mode=clone:stop_duration=2"
    if L <= 0:
        raise RuntimeError("AI 클립 길이를 읽지 못했습니다.")
    if L >= dur - 0.02:
        vf = f"{base},{pad}"
    elif L * max_slow >= dur:
        vf = f"setpts={dur / L:.5f}*PTS,{base},{pad}"
    else:
        head_frames = int(L * max_slow * fps)
        tail_frames = frames - head_frames
        out = Path(out)
        head, tail, last = (out.with_name(out.stem + s) for s in ("_head.mp4", "_tail.mp4", "_last.png"))
        try:
            run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-vf", f"setpts={max_slow}*PTS,{base},{pad}",
                 "-frames:v", str(head_frames), "-r", str(fps), *ENC, str(head)])
            run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.5", "-i", str(raw), "-update", "1",
                 "-frames:v", "1", str(last)])
            kenburns(last, tail, tail_frames, w, h, fps, variant=variant, strength=0.06)
            concat([head, tail], out)
        finally:
            for p in (head, tail, last):
                p.unlink(missing_ok=True)
        return
    run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-vf", vf, "-frames:v", str(frames),
         "-r", str(fps), *ENC, str(out)])


def to_jpeg(image, out, max_side: int = 1280):
    run(["ffmpeg", "-y", "-v", "error", "-i", str(image), "-vf",
         f"scale=w={max_side}:h={max_side}:force_original_aspect_ratio=decrease:force_divisible_by=2",
         "-q:v", "3", "-frames:v", "1", str(out)])


def mux(video, out, total: float, narration=None, bgm=None, bgm_volume: float = 0.12):
    """영상 + 내레이션 + BGM(반복) → 최종 mp4."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video)]
    fc, amap = None, []
    if narration:
        cmd += ["-i", str(narration)]
    if bgm:
        cmd += ["-stream_loop", "-1", "-i", str(bgm)]
    if narration and bgm:
        fc = f"[1:a]apad[n];[2:a]volume={bgm_volume}[b];[n][b]amix=inputs=2:duration=first:normalize=0[a]"
    elif narration:
        fc = "[1:a]apad[a]"
    elif bgm:
        fc = f"[1:a]volume={bgm_volume}[a]"
    if fc:
        cmd += ["-filter_complex", fc]
        amap = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-map", "0:v", *amap, "-c:v", "copy", "-t", f"{math.ceil(total * 1000) / 1000}",
            "-movflags", "+faststart", str(out)]
    run(cmd)
