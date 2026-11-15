"""프로젝트 폴더 → 완성 영상 (final.mp4 + final.srt).

흐름: 내레이션(TTS/파일/없음)으로 타임라인 확정 → 선택 장면은 Grok AI 영상, 나머지는 줌·패닝
→ 장면 길이에 맞춰 다듬기 → 이어 붙이기 → 내레이션·BGM 합성.
중간 결과는 projects/<이름>/clips, tts 폴더에 캐시되어, 다시 실행하면 이어서 진행합니다.
"""
import hashlib
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import ffmpeg_utils as fx
from . import tts as T
from .grok import DEFAULT_MODEL, GrokVideo
from .project import frame_size, load_project, parse_ai_spec, write_srt


def _h(*parts) -> str:
    m = hashlib.sha1()
    for p in parts:
        m.update(p if isinstance(p, bytes) else str(p).encode("utf-8"))
        m.update(b"|")
    return m.hexdigest()[:16]


def _cached(path: Path, key: str) -> bool:
    k = path.with_suffix(path.suffix + ".key")
    return path.exists() and k.exists() and k.read_text() == key


def _mark(path: Path, key: str):
    path.with_suffix(path.suffix + ".key").write_text(key)


def plan(project_dir, ai_spec="30%", grok_max_sec=10, price_per_sec=0.07):
    """실행 전 견적: AI 장면 수, 요청 초, 예상 비용."""
    p = load_project(project_dir)
    ai = parse_ai_spec(ai_spec, [s["idx"] for s in p["scenes"]])
    secs = sum(min(grok_max_sec, max(1, math.ceil(s["end"] - s["start"]))) for s in p["scenes"] if s["idx"] in ai)
    missing = [s["idx"] for s in p["scenes"] if not s["image"]]
    return {"scenes": len(p["scenes"]), "ai_scenes": sorted(ai), "ai_seconds": secs,
            "est_cost": round(secs * price_per_sec, 2), "missing_images": missing,
            "length": p["scenes"][-1]["end"]}


def render(project_dir, *, xai_key="", gemini_key="", ai_spec="30%", grok_model=DEFAULT_MODEL,
           grok_max_sec=10, grok_extra=None, narration="tts", narration_file=None,
           tts_model=T.DEFAULT_TTS_MODEL, tts_voice=T.DEFAULT_VOICE, tts_style=T.DEFAULT_STYLE,
           gap=0.35, bgm_file=None, bgm_volume=0.12, height=720, fps=30, workers=4,
           out_name="final.mp4", only=None, progress=None):
    """only=[번호,...] 이면 그 장면 클립만 만들고 끝낸다(테스트용)."""
    fx.check_ffmpeg()
    say = progress or (lambda stage, done, total: print(f"[{stage}] {done}/{total}", flush=True))
    p = load_project(project_dir)
    d: Path = p["dir"]
    scenes = [dict(s) for s in p["scenes"]]
    if only:
        scenes = [s for s in scenes if s["idx"] in set(only)]
    missing = [s["idx"] for s in scenes if not s["image"]]
    if missing:
        raise RuntimeError(f"이미지가 없는 장면이 있습니다: {missing[:20]} — 앱에서 먼저 생성하세요.")
    W, H = frame_size(p["aspect"], height)
    clips_dir, tts_dir, out_dir = d / "clips", d / "tts", d / "output"
    for x in (clips_dir, tts_dir, out_dir):
        x.mkdir(exist_ok=True)

    # 1) 내레이션 → 타임라인
    narr_path = None
    if narration == "tts" and not only:
        from google import genai
        if not gemini_key:
            raise RuntimeError("TTS에는 Gemini API 키가 필요합니다. (환경변수 GEMINI_API_KEY)")
        client = genai.Client(api_key=gemini_key)
        pcms, todo = {}, []
        for s in scenes:
            f = tts_dir / f'{s["idx"]:03d}.wav'
            key = _h(s["text"], tts_model, tts_voice, tts_style)
            if _cached(f, key):
                pcms[s["idx"]] = T.read_wav(f)
            elif s["text"].strip():
                todo.append((s, f, key))
            else:
                pcms[s["idx"]] = b""
        done = len(scenes) - len(todo)
        say("내레이션(TTS)", done, len(scenes))
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as ex:
            futs = {ex.submit(T.synth, client, s["text"], tts_model, tts_voice, tts_style): (s, f, key)
                    for s, f, key in todo}
            for fut in as_completed(futs):
                s, f, key = futs[fut]
                pcm = fut.result()
                T.write_wav(f, pcm)
                _mark(f, key)
                pcms[s["idx"]] = pcm
                done += 1
                say("내레이션(TTS)", done, len(scenes))
        t, segs = 0.0, []
        for s in scenes:
            pcm = pcms[s["idx"]]
            dur = max(1.5, T.pcm_seconds(pcm) + gap) if pcm else max(1.5, s["end"] - s["start"])
            segs.append((t + gap / 2, pcm))
            s["start"], s["end"] = t, t + dur
            t += dur
        narr_path = out_dir / "narration.wav"
        T.build_track(narr_path, segs)
    elif narration == "file" and narration_file and not only:
        narr_path = Path(narration_file)
        a = fx.duration(narr_path)
        if a > scenes[-1]["end"]:
            scenes[-1]["end"] = a
    if not only:  # 장면 사이 빈틈 없이 이어지게
        for a, b in zip(scenes, scenes[1:]):
            b["start"] = a["end"]
    for s in scenes:
        s["frames"] = max(1, round(s["end"] * fps) - round(s["start"] * fps))

    # 2) 장면 클립
    ai = parse_ai_spec(ai_spec, [s["idx"] for s in scenes])
    grok = GrokVideo(xai_key, grok_model, extra=grok_extra) if ai else None
    total = len(scenes)
    done, failed, ai_ok = [0], {}, []
    results = {}

    def kb(s, variant=None):
        out = clips_dir / f'{s["idx"]:03d}.mp4'
        key = _h("kb", s["image"].read_bytes(), s["frames"], W, H, fps, s["idx"])
        if not _cached(out, key):
            fx.kenburns(s["image"], out, s["frames"], W, H, fps, variant=s["idx"] - 1 if variant is None else variant)
            _mark(out, key)
        return out

    def ai_clip(s):
        req = min(grok_max_sec, max(1, math.ceil(s["frames"] / fps)))
        raw = clips_dir / f'ai_{s["idx"]:03d}_raw.mp4'
        rkey = _h("ai", s["image"].read_bytes(), s["motion"], grok_model, req, grok_extra)
        if not _cached(raw, rkey):
            jpg = clips_dir / f'ai_{s["idx"]:03d}_in.jpg'
            fx.to_jpeg(s["image"], jpg)
            try:
                grok.generate(jpg, s["motion"], req, raw)
            finally:
                jpg.unlink(missing_ok=True)
            _mark(raw, rkey)
        out = clips_dir / f'{s["idx"]:03d}.mp4'
        key = _h("fit", rkey, s["frames"], W, H, fps)
        if not _cached(out, key):
            fx.fit_clip(raw, out, s["frames"], W, H, fps, variant=s["idx"] - 1)
            _mark(out, key)
        return out

    def job(s):
        if s["idx"] in ai:
            try:
                r = ai_clip(s)
                ai_ok.append(s["idx"])
                return r
            except Exception as e:  # noqa: BLE001 - AI 실패 시 줌효과로 대체
                failed[s["idx"]] = str(e)[:300]
        return kb(s)

    say("장면 클립", 0, total)
    cpu = max(1, (os.cpu_count() or 2) - 1)
    # AI 요청은 대기 시간이 길어 많이 병렬, 줌효과는 CPU 작업이라 코어 수만큼
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ai_ex, ThreadPoolExecutor(max_workers=cpu) as kb_ex:
        futs = {(ai_ex if s["idx"] in ai else kb_ex).submit(job, s): s["idx"] for s in scenes}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
            done[0] += 1
            say("장면 클립", done[0], total)

    summary = {"clips": [str(results[s["idx"]]) for s in scenes], "ai_ok": sorted(ai_ok),
               "ai_failed": failed, "size": f"{W}x{H}"}
    if only:
        return summary

    # 3) 합치기
    say("이어 붙이기", 0, 1)
    video_only = out_dir / "_video_only.mp4"
    fx.concat([results[s["idx"]] for s in scenes], video_only)
    total_sec = scenes[-1]["end"]
    final = out_dir / out_name
    say("오디오 합성", 0, 1)
    fx.mux(video_only, final, total_sec, narration=narr_path, bgm=bgm_file, bgm_volume=bgm_volume)
    video_only.unlink(missing_ok=True)
    srt = final.with_suffix(".srt")
    write_srt(scenes, srt)
    say("완료", 1, 1)
    summary.update({"output": str(final), "srt": str(srt), "duration": total_sec})
    return summary
