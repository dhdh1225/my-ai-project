"""대본 → 이미지 자동 생성기 (Streamlit 단일 파일)

파이프라인: 1) 대본 분석 → 2) 초 단위 분할 → 3) 이미지 프롬프트화 → 4) 이미지 병렬 생성
실행: streamlit run app.py
필요: pip install streamlit google-genai
"""
import csv
import hashlib
import hmac
import html
import io
import json
import math
import os
import random
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import streamlit as st

# ───────────────────────── 상수 ─────────────────────────
IMAGE_MODELS = {
    "나노바나나 프로": "gemini-3-pro-image-preview",
    "나노바나나2": "gemini-3.1-flash-image-preview",
}
DEFAULT_TEXT_MODEL = "gemini-2.5-flash"
ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4"]
SCENE_SECONDS = [3, 4, 5, 6, 10, 15, 20]
IMAGE_LANGS = {"한국어": "Korean (Hangul)", "일본어": "Japanese", "영어": "English", "언어 없음": None}
MAX_REFS = 5
SEC_PER_IMAGE_GUESS = 15  # 예상 시간 계산용 가정값 (실제와 다를 수 있음)

JIGU_STYLE = (
    "A clean, fully realized 2D vector illustration suitable for an educational "
    "history documentary. The style uses consistent bold black outlines and flat, "
    "muted colors with minimal shading. The composition is visually dense and "
    "multilayered, with the background fully populated by environmental elements "
    "appropriate to the specific time period and setting described. No empty "
    "negative space. The scene depicts"
)
BUILTIN_PRESETS = {"지구박물관 (기본)": JIGU_STYLE}
CUSTOM_LABEL = "직접 입력"

DEFAULT_FORMAT_PROMPT = (
    "각 장면 묘사는 영어 한 문단(2~3문장)으로, 아래 순서를 따른다.\n"
    "[시대/장소] → [중심 인물과 행동] → [주변 소품·건축·배경 요소] → [구도(카메라 거리/각도)]\n"
    "- 같은 인물/장소는 전 장면에서 같은 외형·복장으로 일관되게 묘사한다.\n"
    "- 대본에 없는 사실을 지어내지 않는다."
)

DEFAULT_MOTION_RULE = (
    "영어로, 장면당 1~2문장의 이미지-투-비디오용 움직임 지시문을 쓴다.\n"
    "- 카메라 움직임(천천히 확대/축소/좌우 이동)과 눈에 띄는 요소 1~2개의 절제된 움직임만 서술한다.\n"
    "- 원본 이미지의 2D 벡터 스타일, 선 굵기, 색감, 구도를 유지하고 새로운 인물·사물·글자를 추가하지 않는다.\n"
    "- 지정된 장면 길이 안에 끝날 만큼만 움직이게 한다.\n"
    "- 대사·음성·배경음악·효과음이 없는 무음 영상으로 지시한다."
)
SILENT_DIRECTIVE = ("No dialogue, no speech, no voiceover, no characters talking or lip-syncing, "
                    "no background music, no sound effects. Silent video.")
NAME_RULES = {
    "scene_번호 (scene_001)": "scene_{n:03d}",
    "번호만 (001)": "{n:03d}",
}

RETRIABLE_HINTS = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL", "timed out", "timeout")

PROJECTS_DIR = Path("projects")
PRESETS_FILE = Path("presets.json")

# 프로젝트에 저장/복원하는 입력 위젯 키 (API 키는 저장하지 않음)
INPUT_KEYS = ["script", "format_prompt", "style", "preset", "model_label", "text_model", "aspect",
              "sec", "cps", "lang", "workers", "motion_rule", "name_rule", "silent_rule"] + [f"mid_{k}" for k in IMAGE_MODELS]


# ───────────────────────── 유틸 ─────────────────────────
def lang_directive(lang: str) -> str:
    """이미지 속 글자 언어 규칙 (영문 지시문)."""
    name = IMAGE_LANGS.get(lang)
    if name is None:
        return ("The image must contain no text of any kind: no letters, numbers, captions, "
                "signs with writing, logos, watermarks or speech bubbles.")
    return ("Never add subtitles, captions, logos, watermarks or speech bubbles. "
            f"The only text allowed is minimal in-scene writing (signs, labels, inscriptions), "
            f"which must be written in {name} only and spelled correctly.")


def ensure_silent(text: str, directive: str) -> str:
    """움직임 프롬프트에 무음 지시문이 없으면 뒤에 붙인다 (이미 있으면 그대로)."""
    text, directive = (text or "").strip(), (directive or "").strip()
    if not text or not directive or directive.lower() in text.lower():
        return text
    return f"{text} {directive}"


def img_ext(b: bytes) -> str:
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:4] == b"RIFF":
        return "webp"
    return "png"


def safe_name(n: str) -> str:
    n = re.sub(r"[^\w\-]+", "_", (n or "").strip(), flags=re.UNICODE).strip("_")
    return n or "project"


def script_hash(text: str) -> str:
    import hashlib
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()


def get_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


def call_text_json(client, model: str, prompt: str):
    """텍스트 모델을 JSON 모드로 호출하고 파싱한다 (재시도 포함)."""
    from google.genai import types
    last = None
    for attempt in range(4):
        try:
            res = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4),
            )
            txt = (res.text or "").strip()
            txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.M).strip()
            return json.loads(txt)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"텍스트 모델 호출 실패: {last}")


# ───────────────────────── 1단계: 대본 분석 ─────────────────────────
def analyze_script(client, model: str, script: str) -> dict:
    prompt = f"""다음 대본을 분석해 JSON 객체 하나만 출력하라. 키:
- "summary": 대본 한 줄 요약 (한국어)
- "era": 주요 시대
- "locations": 주요 장소 배열
- "characters": 등장 인물/집단과 외형 특징 배열 (복장, 연령대 등 일관 묘사용, 영어)
- "key_props": 핵심 소품·건축·유물 배열 (영어)
- "mood": 분위기 (영어)

대본:
{script}"""
    data = call_text_json(client, model, prompt)
    return data if isinstance(data, dict) else {"summary": str(data)}


# ───────────────────────── 2단계: 초 단위 분할 ─────────────────────────
_SENT_SPLIT = re.compile(r'(?<=[.!?。…])\s+|(?<=[.!?。…]["\'”’)\]」』])\s+|\n+')
_SENT_END = re.compile(r"[.!?。…]")


def _units(text: str):
    """문장 단위로만 나눈다. 마침표(. ! ? 。 …)로 끝나는 지점에서만 끊고, 문장 중간은 자르지 않는다."""
    return [u.strip() for u in _SENT_SPLIT.split(text) if u and u.strip()]


def _soft_units(text: str, max_len: int):
    """문장 부호가 하나도 없는 아주 긴 덩어리만 예외적으로 쉼표/공백에서 나눈다."""
    out, buf = [], ""
    for tok in re.split(r"(?<=[,，、])\s*|\s+", text):
        if not tok:
            continue
        if buf and len(buf) + len(tok) + 1 > max_len:
            out.append(buf)
            buf = tok
        else:
            buf = f"{buf} {tok}".strip()
    if buf:
        out.append(buf)
    return out


def _nsp(t: str) -> int:
    return len(re.sub(r"\s", "", t))


def estimate(script: str, sec_per_scene: int, cps: float):
    """(공백 제외 글자 수, 추정 초, 장면 수)"""
    chars = _nsp(script)
    if chars == 0:
        return 0, 0.0, 0
    total = chars / cps
    return chars, total, max(1, math.ceil(total / sec_per_scene))


def split_script(script: str, sec_per_scene: int, chars_per_sec: float):
    """대본을 '장면당 sec_per_scene초' 분량에 가깝게 나눈다.
    장면은 항상 문장이 끝나는 곳(마침표 등)에서만 끊긴다. 문장이 길면 그 장면은 목표보다 조금 길어진다."""
    text = script.strip()
    total_chars, total_sec, n = estimate(text, sec_per_scene, chars_per_sec)
    if total_chars == 0:
        return [], 0.0
    target = total_chars / n  # 장면당 목표 글자 수 (공백 제외)

    units = []
    for u in _units(text):
        # 문장 부호가 전혀 없는데 아주 긴 덩어리일 때만 예외적으로 더 쪼갠다
        if not _SENT_END.search(u) and _nsp(u) > 2 * target:
            units.extend(_soft_units(u, max(10, int(target * 1.6))))
        else:
            units.append(u)

    groups, cur, cur_len = [], [], 0
    for u in units:
        ln = _nsp(u)
        if cur and cur_len + ln / 2 > target:  # 이 문장을 더하면 목표를 더 많이 넘기면 여기서 끊는다
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(u)
        cur_len += ln
    if cur:
        groups.append(cur)

    scenes, t = [], 0.0
    for i, g in enumerate(groups, 1):
        body = " ".join(g)
        dur = _nsp(body) / chars_per_sec
        scenes.append({"idx": i, "start": t, "end": t + dur, "text": body})
        t += dur
    return scenes, total_sec


# ───────────────────────── 3단계: 이미지 프롬프트화 ─────────────────────────
def build_scene_descriptions(client, model, analysis, scenes, format_prompt, lang, progress_cb=None, batch=15):
    ctx = json.dumps(analysis, ensure_ascii=False)
    result = {}
    for b in range(0, len(scenes), batch):
        chunk = scenes[b:b + batch]
        items = "\n".join(f'{s["idx"]}. {s["text"]}' for s in chunk)
        prompt = f"""너는 역사 다큐멘터리 일러스트 장면 기획자다.
아래 [공통 컨텍스트]와 [형식 규칙]을 반드시 따라, 각 장면 대본에 대응하는 '이미지 장면 묘사'를 영어로 작성하라.
이미지 스타일 문구는 따로 붙으므로 묘사에는 스타일 설명을 쓰지 말고, 장면 내용만 서술하라.
출력은 JSON 배열만: [{{"id": 장면번호(정수), "scene": "영어 장면 묘사"}}, ...]  (입력 장면 수와 동일하게)

[공통 컨텍스트]
{ctx}

[형식 규칙]
{format_prompt}

[이미지 속 글자 규칙]
{lang_directive(lang)}

[장면 대본]
{items}"""
        data = call_text_json(client, model, prompt)
        if isinstance(data, dict):
            data = data.get("scenes") or data.get("items") or list(data.values())[0]
        for row in data:
            try:
                result[int(row["id"])] = str(row["scene"]).strip()
            except (KeyError, ValueError, TypeError):
                continue
        if progress_cb:
            progress_cb(min(len(scenes), b + batch), len(scenes))
    return result


def compose_prompt(style: str, scene_desc: str) -> str:
    return f"{style.strip()} {scene_desc.strip()}".strip()


def build_motion_prompts(client, model, analysis, scenes, motion_rule, progress_cb=None, batch=15):
    """장면별 이미지-투-비디오용 움직임 프롬프트를 만든다. {장면번호: 프롬프트}"""
    ctx = json.dumps(analysis or {}, ensure_ascii=False)
    result = {}
    for b in range(0, len(scenes), batch):
        chunk = scenes[b:b + batch]
        items = "\n".join(
            f'{s["idx"]}. (길이 {max(1, round(s["end"] - s["start"]))}초) 대본: {s["text"]} / 장면 묘사: {s.get("desc") or s["text"]}'
            for s in chunk)
        prompt = f"""너는 정지 이미지를 짧은 영상으로 만드는 애니메이션 연출가다.
각 장면 이미지에 적용할 '움직임 프롬프트'를 아래 [움직임 규칙]에 맞게 작성하라.
출력은 JSON 배열만: [{{"id": 장면번호(정수), "motion": "영어 움직임 프롬프트"}}, ...]  (입력 장면 수와 동일하게)

[공통 컨텍스트]
{ctx}

[움직임 규칙]
{motion_rule}

[장면 목록]
{items}"""
        data = call_text_json(client, model, prompt)
        if isinstance(data, dict):
            data = data.get("scenes") or data.get("items") or list(data.values())[0]
        for row in data:
            try:
                result[int(row["id"])] = str(row["motion"]).strip()
            except (KeyError, ValueError, TypeError):
                continue
        if progress_cb:
            progress_cb(min(len(scenes), b + batch), len(scenes))
    return result


# ───────────────────────── 4단계: 이미지 병렬 생성 ─────────────────────────
class Throttle:
    """동시 실행 수 제한. 429가 감지되면 한도를 자동으로 1씩 낮춘다."""

    def __init__(self, limit: int):
        self.limit, self.initial, self.active, self.cuts = limit, limit, 0, 0
        self.cv = threading.Condition()
        self.last_cut = 0.0

    def __enter__(self):
        with self.cv:
            while self.active >= self.limit:
                self.cv.wait()
            self.active += 1

    def __exit__(self, *exc):
        with self.cv:
            self.active -= 1
            self.cv.notify_all()

    def penalize(self):
        with self.cv:
            now = time.time()
            if self.limit > 1 and now - self.last_cut > 5:
                self.limit -= 1
                self.cuts += 1
                self.last_cut = now


def generate_one(client, model: str, prompt: str, aspect: str, refs, throttle: Throttle):
    """워커 스레드에서 실행 (Streamlit 호출 금지). (bytes|None, error|None) 반환."""
    from google.genai import types
    contents = [prompt] + [types.Part.from_bytes(data=r["data"], mime_type=r["mime"]) for r in refs]
    last = ""
    for attempt in range(5):
        try:
            with throttle:  # 대기(백오프) 중에는 슬롯을 점유하지 않음
                res = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=types.ImageConfig(aspect_ratio=aspect),
                    ),
                )
            for cand in (res.candidates or []):
                for part in (getattr(cand.content, "parts", None) or []):
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        return inline.data, None
            reason = str(getattr(res.candidates[0], "finish_reason", "")) if res.candidates else ""
            last = f"이미지가 반환되지 않음 (안전 필터 등) {reason}".strip()
            if attempt >= 1:  # 필터 거절은 1회만 재시도
                break
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if "429" in last or "RESOURCE_EXHAUSTED" in last:
                throttle.penalize()
            if not any(h.lower() in last.lower() for h in RETRIABLE_HINTS):
                break  # 재시도해도 소용없는 오류(키 오류 등)
        time.sleep(min(30, 2 ** attempt) + random.random())  # 지수 백오프
    return None, last[:300]


def run_parallel(client, model, aspect, targets, workers, on_done, refs, throttle):
    """완료되는 순서대로 on_done(scene_idx, bytes, err) 호출 (메인 스레드).
    사용자가 중단하면(스크립트가 중단되면) 아직 시작하지 않은 장면은 취소한다."""
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(generate_one, client, model, s["gen_prompt"], aspect, refs, throttle): s["idx"]
                for s in targets}
        for f in as_completed(futs):
            idx = futs[f]
            try:
                img, err = f.result()
            except Exception as e:  # noqa: BLE001
                img, err = None, str(e)[:300]
            on_done(idx, img, err)
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)  # 대기 중인 장면 취소 후 중단을 그대로 전달
        raise
    finally:
        ex.shutdown(wait=False)


# ───────────────────────── 파일 입출력 (자동 저장/프리셋) ─────────────────────────
def load_custom_presets() -> dict:
    try:
        return json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_custom_presets(d: dict):
    PRESETS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def list_projects():
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.name for p in PROJECTS_DIR.iterdir() if (p / "project.json").exists())


# ───────────────────────── 결과 보조 ─────────────────────────
def current_psig() -> str:
    """프롬프트를 만들 때 쓴 스타일·규칙·언어·모델의 서명. 바뀌면 그 장면 프롬프트는 낡은 것으로 본다."""
    st_ = st.session_state
    raw = json.dumps([st_.get("style"), st_.get("format_prompt"), st_.get("lang"), st_.get("text_model")],
                     ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def compose_gen_prompt(sc) -> str:
    """이미지 생성에 실제로 보내는 프롬프트 (글자 언어 규칙 + 레퍼런스 안내 포함)."""
    st_ = st.session_state
    hint = (" Match the character design, outfit and key objects of the attached reference image(s) exactly."
            if st_.get("refs") else "")
    return f'{sc["prompt"].strip()} {lang_directive(st_.get("lang", "언어 없음"))}{hint}'


def _paint_image(img_slot, sc, busy: bool):
    with img_slot.container():
        if sc.get("image"):
            try:
                st.image(sc["image"], width="stretch")
            except Exception:  # noqa: BLE001 - 깨진 이미지 하나로 화면이 죽지 않게
                st.markdown('<div class="ph fail">⚠️<br>이미지를 표시할 수 없음</div>', unsafe_allow_html=True)
        elif sc.get("error"):
            st.markdown('<div class="ph fail">⚠️<br>생성 실패</div>', unsafe_allow_html=True)
            st.caption(sc["error"][:140])
        elif busy:
            st.markdown('<div class="ph busy">🔄<br>이미지 생성 중…<br><small>GENERATING AI VISION</small></div>',
                        unsafe_allow_html=True)
        else:
            st.markdown('<div class="ph">🖼️<br>아직 만들지 않음</div>', unsafe_allow_html=True)


def _ensure_prompt(sc):
    """이 장면의 프롬프트가 아직 없으면 지금 이 장면 것만 만든다 (대본 분석은 한 번만)."""
    st_ = st.session_state
    if sc.get("prompt"):
        return
    client = get_client(st_.api_key)
    if not st_.analysis:
        st_.analysis = analyze_script(client, st_.text_model, st_.script)
        st_.analysis_hash = script_hash(st_.script)
    descs = build_scene_descriptions(client, st_.text_model, st_.analysis or {}, [sc], st_.format_prompt, st_.lang)
    desc = descs.get(sc["idx"]) or sc["text"]
    sc["desc"], sc["prompt"], sc["psig"] = desc, compose_prompt(st_.style, desc), current_psig()
    if st_.get("motion_auto"):
        res = build_motion_prompts(client, st_.text_model, st_.analysis, [sc], st_.motion_rule)
        base = res.get(sc["idx"]) or "Slow gentle zoom in, subtle ambient movement, keep the illustration style unchanged."
        sc["motion"] = ensure_silent(base, st_.silent_rule)


def _regen_inline(img_slot, sc):
    """한 장면만 그 자리에서 만든다. 프롬프트가 아직 없으면 먼저 만들고, 이어서 이미지를 만든다."""
    st_ = st.session_state
    if not st_.get("api_key"):
        st.error("사이드바에 Gemini API 키를 입력하세요.")
        return
    had_image = bool(sc.get("image"))
    if not sc.get("prompt"):
        with img_slot.container():
            st.markdown('<div class="ph busy">✍️<br>프롬프트 작성 중…<br><small>WRITING PROMPT</small></div>',
                        unsafe_allow_html=True)
        try:
            _ensure_prompt(sc)
        except Exception as e:  # noqa: BLE001
            sc["error"] = f"프롬프트 작성 실패: {e}"[:300]
            st.toast(f"#{sc['idx']} {sc['error'][:80]}")
            _paint_image(img_slot, sc, False)
            return
    with img_slot.container():
        msg = "다시 생성 중…" if had_image else "이미지 생성 중…"
        st.markdown(f'<div class="ph busy">🔄<br>{msg}<br><small>GENERATING AI VISION</small></div>',
                    unsafe_allow_html=True)
    sc["gen_prompt"] = compose_gen_prompt(sc)
    img, err = generate_one(get_client(st_.api_key), st_[f"mid_{st_.model_label}"], sc["gen_prompt"],
                            st_.aspect, st_.refs, Throttle(1))
    if img:
        sc["image"] = img
        sc.pop("error", None)
        if st_.get("autosave"):
            save_image(sc)
        st.rerun()  # 다운로드 버튼 등이 새 이미지를 가리키도록 화면을 새로 그린다
    sc["error"] = err
    st.toast(f"#{sc['idx']} 생성 실패: {(err or '')[:80]}")
    _paint_image(img_slot, sc, False)


_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def render_scene_row(slot, sc, interactive: bool = False, busy: bool = False):
    """장면 카드: 왼쪽 이미지(+다운로드) | 오른쪽 SCENE·상태·NARRATION SCRIPT·IMAGE PROMPT·다시 만들기."""
    st_ = st.session_state
    portrait = st_.get("aspect") in ("9:16", "3:4")
    if sc.get("image"):
        kind, label = "done", "완성됨"
    elif sc.get("error"):
        kind, label = "fail", "실패"
    elif busy:
        kind, label = "prog", "엔진 가동 중…"
    else:
        kind, label = "wait", "대기 중"
    script_html = html.escape(sc["text"]).replace("\n", "<br>")
    prompt_html = (f'<div class="lbl">IMAGE PROMPT</div><div class="prompt-box">'
                   f'{html.escape(sc.get("prompt", "")).replace(chr(10), "<br>")}</div>') if sc.get("prompt") else ""
    motion_html = (f'<div class="lbl">ANIMATION PROMPT</div><div class="prompt-box">'
                   f'{html.escape(ensure_silent(sc.get("motion", ""), st_.get("silent_rule", ""))).replace(chr(10), "<br>")}'
                   f'</div>') if sc.get("motion") else ""
    with slot.container(border=True):
        left, right = st.columns([2, 3] if portrait else [5, 4], vertical_alignment="center")
        with left:
            img_slot = st.empty()
            _paint_image(img_slot, sc, busy)
            if interactive and sc.get("image"):
                ext = img_ext(sc["image"])
                st.download_button(":material/download: 고화질 다운로드", sc["image"],
                                   file_name=f'{scene_filename(sc, st_.get("name_rule", ""))}.{ext}',
                                   mime=_MIME.get(ext, "image/png"), key=f"dl_{sc['idx']}", width="stretch")
        with right:
            st.markdown(
                f'<div class="scene-head"><span><span class="scene-badge">SCENE {sc["idx"]}</span>'
                f'</span>'
                f'<span class="chip {kind}">{label}</span></div>'
                f'<div class="lbl">NARRATION SCRIPT</div><div class="scene-script">{script_html}</div>{prompt_html}{motion_html}',
                unsafe_allow_html=True)
            btn = (":material/refresh: 이 장면 다시 만들기" if sc.get("image")
                   else ":material/auto_awesome: 이 장면 생성")
            if interactive and st.button(btn, key=f"regen_{sc['idx']}", width="stretch"):
                _regen_inline(img_slot, sc)


def render_rows(container, scenes, interactive: bool = False):
    """장면 1..N을 위에서 아래로 카드 한 줄씩 그린다."""
    for sc in scenes:
        render_scene_row(container.empty(), sc, interactive)


def scene_filename(sc, rule: str) -> str:
    pat = NAME_RULES.get(rule, next(iter(NAME_RULES.values())))
    return pat.format(n=sc["idx"])


def make_zip(scenes, rule: str = "", with_motion: bool = False, silent: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for s in scenes:
            if s.get("image"):
                base = scene_filename(s, rule)
                z.writestr(f'{base}.{img_ext(s["image"])}', s["image"])
                if with_motion and s.get("motion"):
                    z.writestr(f"{base}.txt", ensure_silent(s["motion"], silent))
    return buf.getvalue()


def make_csv(scenes, silent: str = "") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["번호", "대본", "이미지 프롬프트", "애니메이션 프롬프트", "상태"])
    for s in scenes:
        status = "성공" if s.get("image") else ("실패: " + s["error"] if s.get("error") else "미생성")
        w.writerow([s["idx"], s["text"], s.get("gen_prompt") or s.get("prompt", ""),
                    ensure_silent(s.get("motion", ""), silent), status])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def make_motion_txt(scenes, silent: str = "") -> bytes:
    out = [f'#{s["idx"]}\n{ensure_silent(s.get("motion", ""), silent)}\n' for s in scenes]
    return "\n".join(out).encode("utf-8")


# ───────────────────────── 앱 ─────────────────────────
st.set_page_config(page_title="대본 → 이미지 생성기", page_icon="🖼️", layout="wide")
ss = st.session_state

# 색은 아래 :root 변수만 바꾸면 전체 화면에 반영됩니다.
THEME_CSS = """
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css');
:root {
  --bg: #ffffff;            /* 본문 배경 */
  --side: #f6f3f0;          /* 사이드바 배경 (따뜻한 오프화이트) */
  --soft: #f9dcd7;          /* 포인트 살구 (제목 밑줄 등) */
  --accent: #d9958a;        /* 강조 (더스티 살구: 진행바·포커스·슬라이더) */
  --accent-deep: #6b615b;   /* 마우스 올렸을 때 글씨 (웜그레이) */
  --cta: #efb9ad;           /* 주요 버튼 */
  --text: #3d3330;          /* 글씨 (따뜻한 차콜) */
  --muted: #8a7a76;         /* 보조 글씨 */
  --line: #e6e0db;          /* 테두리 (웜그레이) */
  --ink: #2f2724;           /* 주요 버튼 (딥 차콜) */
}
html, body, .stApp, .stApp *:not([data-testid="stIconMaterial"]):not([class*="material" i]):not([class*="icon" i]) {
  font-family: 'Pretendard', -apple-system, 'Segoe UI', 'Noto Sans KR', sans-serif; }
.stApp { background: var(--bg); color: var(--text); }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stSidebar"] { background: var(--side); border-right: 1px solid var(--line); }
h1, h2, h3 { color: var(--text) !important; letter-spacing: -0.02em; font-weight: 700; }
h1 { border-bottom: 3px solid var(--soft); padding-bottom: .35rem; display: inline-block; }
[data-testid="stCaptionContainer"], .stCaption { color: var(--muted) !important; }

/* 버튼: 흰색(보조) · 차콜(주요/카드) · 연한 레드(중단) */
.stButton button, .stDownloadButton button {
    min-height: 2.6rem; padding: .4rem 1rem; border-radius: 10px;
    background: #fff; color: var(--text); border: 1px solid var(--line);
    font-weight: 500; font-size: .92rem; letter-spacing: -.01em;
    box-shadow: 0 1px 2px rgba(61, 51, 48, .05);
    transition: background .15s ease, border-color .15s ease, box-shadow .15s ease, transform .1s ease; }
.stButton button:hover, .stDownloadButton button:hover {
    background: #faf7f5; border-color: #cdbfb8; color: var(--text); box-shadow: 0 2px 8px rgba(61, 51, 48, .08); }
.stButton button:active, .stDownloadButton button:active { transform: translateY(1px); box-shadow: none; }
.stButton button:disabled { opacity: .45; }
.stButton button[kind="primary"] {
    background: var(--ink) !important; color: #fff !important; border: 1px solid var(--ink) !important;
    min-height: 2.9rem; border-radius: 12px; font-weight: 600; box-shadow: 0 6px 16px rgba(47, 39, 36, .20); }
.stButton button[kind="primary"]:hover { background: #46393a !important; border-color: #46393a !important; }
.stButton button[kind="primary"] [data-testid="stIconMaterial"] { color: var(--cta); }

/* 입력창 */
textarea, input { background: #fff !important; color: var(--text) !important; }
[data-baseweb="textarea"], [data-baseweb="input"], [data-baseweb="base-input"] {
    background: #fff !important; border-radius: 10px !important; border-color: var(--line) !important; }
[data-baseweb="textarea"]:focus-within, [data-baseweb="input"]:focus-within {
    border-color: var(--accent) !important; box-shadow: 0 0 0 3px rgba(217, 149, 138, .18) !important; }
[data-baseweb="select"] > div { background: #fff !important; border-radius: 10px !important; border-color: var(--line) !important; }
[data-testid="stFileUploaderDropzone"] { background: #faf8f6; border: 1px dashed #d6cec7; border-radius: 12px; }

/* 확장 패널 · 표 · 진행바 · 슬라이더 · 이미지 */
[data-testid="stExpander"] { background: #fff; border: 1px solid var(--line); border-radius: 12px; }
[data-testid="stDataFrame"], [data-testid="stDataEditor"] { border-radius: 10px; border: 1px solid var(--line); }
.stProgress > div > div > div > div { background-color: var(--accent) !important; }
[data-testid="stSlider"] [role="slider"] { background-color: var(--accent) !important; }
[data-testid="stImage"] img { border-radius: 10px; box-shadow: 0 2px 10px rgba(61, 51, 48, .10); }
hr { border-color: var(--line) !important; }

/* 장면 카드 */
[data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--line) !important; border-radius: 14px !important; background: #fff; }
.scene-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: .55rem; }
.scene-badge { background: var(--ink); color: #fff; font-weight: 700; letter-spacing: .06em; font-size: .72rem;
    padding: .28rem .6rem; border-radius: 6px; }
.scene-time { color: var(--muted); font-size: .8rem; margin-left: .6rem; }
.chip { font-size: .75rem; padding: .2rem .7rem; border-radius: 999px; font-weight: 600; }
.chip.done { background: #e6f0e8; color: #3f6b4b; }
.chip.prog { background: var(--soft); color: var(--accent-deep); }
.chip.fail { background: #fbe4e0; color: #a33a2f; }
.chip.wait { background: #efeae6; color: var(--muted); }
.lbl { font-size: .7rem; letter-spacing: .12em; color: var(--muted); font-weight: 700; margin: .1rem 0 .2rem; }
.ph { min-height: 170px; display: flex; flex-direction: column; align-items: center; justify-content: center;
    background: var(--side); border: 1px dashed #d6cec7; border-radius: 10px; color: var(--muted);
    font-size: .95rem; text-align: center; line-height: 1.8; }
.ph.fail { background: #fff4f2; border-color: #f0c4bd; color: #a33a2f; }
.ph.busy { background: #3d3330; border: 1px solid #3d3330; color: #f1e8e4; font-weight: 600; }
.ph small { letter-spacing: .16em; opacity: .6; font-size: .6rem; font-weight: 500; }
.scene-script { font-size: 1.02rem; font-weight: 600; line-height: 1.65; color: var(--text); margin: .05rem 0 .7rem; }
.prompt-box { background: var(--side); border: 1px solid var(--line); border-radius: 10px; padding: .6rem .75rem;
    font-size: .74rem; line-height: 1.55; color: var(--muted); max-height: 128px; overflow-y: auto; margin-bottom: .7rem; }
[class*="st-key-regen_"] button { background: var(--ink) !important; color: #fff !important; border-color: var(--ink) !important;
    font-weight: 600; }
[class*="st-key-regen_"] button:hover { background: #46393a !important; border-color: #46393a !important; }
[class*="st-key-regen_"] [data-testid="stIconMaterial"] { color: var(--cta); }
[class*="st-key-stop_btn"] button { background: #fff !important; color: #b4443a !important; border: 1px solid #efc9c4 !important; }
[class*="st-key-stop_btn"] button:hover { background: #fdeeed !important; border-color: #e5a9a2 !important; }
</style>
"""
st.markdown(THEME_CSS, unsafe_allow_html=True)


def _secret(name: str) -> str:
    """환경변수 → Streamlit secrets 순으로 값을 찾는다."""
    val = os.environ.get(name) or ""
    if not val:
        try:
            val = str(st.secrets.get(name, "") or "")
        except Exception:  # noqa: BLE001 - secrets 파일이 없을 때
            val = ""
    return val


def _default_api_key() -> str:
    return _secret("GEMINI_API_KEY") or _secret("GOOGLE_API_KEY")


# 배포 시 접근 제한: APP_PASSWORD가 설정돼 있으면 비밀번호를 맞혀야 앱이 열린다.
_app_pw = _secret("APP_PASSWORD")
if _app_pw and not ss.get("_authed"):
    st.title(":material/lock: 로그인")
    _pw = st.text_input("비밀번호", type="password")
    if _pw:
        if hmac.compare_digest(_pw.encode("utf-8"), _app_pw.encode("utf-8")):
            ss._authed = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    st.stop()


DEFAULTS = {"script": "", "format_prompt": DEFAULT_FORMAT_PROMPT, "style": JIGU_STYLE,
            "preset": "지구박물관 (기본)", "model_label": "나노바나나 프로", "text_model": DEFAULT_TEXT_MODEL,
            "aspect": "16:9", "sec": 5, "cps": 7.0, "lang": "언어 없음", "workers": 4,
            "api_key": _default_api_key(), "project_name": "my_project", "autosave": True,
            "motion_rule": DEFAULT_MOTION_RULE, "name_rule": list(NAME_RULES)[0], "zip_motion": False, "silent_rule": SILENT_DIRECTIVE, "motion_auto": True, "new_preset_name": "", "load_choice": "(선택)",
            "log": [], "analysis": None, "analysis_hash": None, "scenes": [], "total_sec": 0.0, "refs": [],
            **{f"mid_{k}": v for k, v in IMAGE_MODELS.items()}}
for _k, _v in DEFAULTS.items():
    ss.setdefault(_k, _v)


def project_dir() -> Path:
    return PROJECTS_DIR / safe_name(ss.project_name)


def save_project(full: bool = False):
    """프로젝트 JSON 저장. full=True면 이미지/참조 이미지 파일도 전부 다시 쓴다."""
    try:
        d = project_dir()
        d.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "inputs": {k: ss.get(k) for k in INPUT_KEYS}, "analysis": ss.analysis,
                "analysis_hash": ss.analysis_hash, "total_sec": ss.total_sec,
                "scenes": [{k: v for k, v in s.items() if k != "image"} for s in ss.scenes]}
        (d / "project.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        if full:
            (d / "images").mkdir(exist_ok=True)
            keep = set()
            for s in ss.scenes:
                if s.get("image"):
                    p = d / "images" / f'{s["idx"]:03d}.{img_ext(s["image"])}'
                    p.write_bytes(s["image"])
                    keep.add(p.name)
            for f in (d / "images").iterdir():
                if f.name not in keep:
                    f.unlink()
            (d / "refs").mkdir(exist_ok=True)
            for f in (d / "refs").iterdir():
                f.unlink()
            for i, r in enumerate(ss.refs, 1):
                (d / "refs" / f'{i}_{safe_name(r["name"])}.{r["mime"].split("/")[-1]}').write_bytes(r["data"])
    except Exception as e:  # noqa: BLE001
        st.toast(f"자동 저장 실패: {e}")


def save_image(s):
    try:
        d = project_dir() / "images"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob(f'{s["idx"]:03d}.*'):
            old.unlink()
        (d / f'{s["idx"]:03d}.{img_ext(s["image"])}').write_bytes(s["image"])
    except Exception:  # noqa: BLE001
        pass


def load_project():
    choice = ss.get("load_choice")
    if not choice or choice == "(선택)":
        return
    d = PROJECTS_DIR / choice
    try:
        data = json.loads((d / "project.json").read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        st.toast(f"불러오기 실패: {e}")
        return
    options = list(BUILTIN_PRESETS) + list(load_custom_presets()) + [CUSTOM_LABEL]
    for k, v in (data.get("inputs") or {}).items():
        if v is None or k not in INPUT_KEYS:
            continue
        if k == "sec" and v not in SCENE_SECONDS:
            continue
        if k == "lang" and v not in IMAGE_LANGS:
            continue
        if k == "model_label" and v not in IMAGE_MODELS:
            continue
        if k == "name_rule" and v not in NAME_RULES:
            continue
        if k == "preset" and v not in options:
            v = CUSTOM_LABEL
        ss[k] = v
    ss.project_name = choice
    ss.analysis, ss.analysis_hash = data.get("analysis"), data.get("analysis_hash")
    ss.total_sec = data.get("total_sec", 0.0)
    scenes = data.get("scenes", [])
    for s in scenes:
        for p in (d / "images").glob(f'{s["idx"]:03d}.*'):
            s["image"] = p.read_bytes()
            break
    ss.scenes = scenes
    ss.refs = []
    if (d / "refs").exists():
        for p in sorted((d / "refs").iterdir())[:MAX_REFS]:
            ss.refs.append({"name": p.name, "data": p.read_bytes(), "mime": f"image/{p.suffix[1:] or 'png'}"})
    st.toast(f"'{choice}' 불러옴 (장면 {len(scenes)}개)")


def on_preset_change():
    name = ss.preset
    if name == CUSTOM_LABEL:
        return
    allp = {**BUILTIN_PRESETS, **load_custom_presets()}
    if name in allp:
        ss.style = allp[name]


def save_preset():
    nm = (ss.new_preset_name or "").strip()
    if not nm or not ss.style.strip():
        st.toast("프리셋 이름과 스타일 내용을 입력하세요.")
        return
    if nm in BUILTIN_PRESETS:
        st.toast("기본 프리셋과 같은 이름은 쓸 수 없습니다.")
        return
    cp = load_custom_presets()
    cp[nm] = ss.style
    save_custom_presets(cp)
    ss.preset, ss.new_preset_name = nm, ""
    st.toast(f"프리셋 '{nm}' 저장됨")


def delete_preset():
    cp = load_custom_presets()
    if ss.preset in cp:
        del cp[ss.preset]
        save_custom_presets(cp)
        st.toast(f"프리셋 '{ss.preset}' 삭제됨")
        ss.preset = CUSTOM_LABEL


# ───────────────────────── 사이드바 ─────────────────────────
with st.sidebar:
    st.header(":material/settings: 설정")
    st.text_input("Gemini API 키", type="password", key="api_key",
                  help="저장되지 않습니다. 환경변수 GEMINI_API_KEY 또는 Streamlit secrets에 두면 자동 입력됩니다.")
    st.selectbox("이미지 모델", list(IMAGE_MODELS), key="model_label")
    st.selectbox("화면 비율", ASPECT_RATIOS, key="aspect")
    st.selectbox("장면당 길이 (초 단위 분할)", SCENE_SECONDS, key="sec", format_func=lambda x: f"{x}초",
                 help="대본을 이 길이(초) 분량으로 나눠 장면 하나로 만듭니다. 분량은 '고급 설정'의 낭독 속도로 계산합니다.")
    st.selectbox("이미지 속 글자 언어", list(IMAGE_LANGS), key="lang",
                 help="기본은 '언어 없음'(글자·자막·로고 없음). 언어를 고르면 간판·비문 같은 장면 속 글자만 "
                      "해당 언어로 허용하고 자막·로고·말풍선은 계속 금지합니다.")

    with st.expander(":material/tune: 고급 설정"):
        st.text_input("이미지 모델 ID", key=f"mid_{ss.model_label}", help="모델 ID가 바뀌면 여기서 수정하세요.")
        st.text_input("분석·프롬프트화 텍스트 모델", key="text_model")
        st.number_input("낭독 속도 (글자/초)", 3.0, 15.0, step=0.5, key="cps",
                        help="대본을 '장면당 N초' 분량으로 나눌 때 쓰는 기준입니다. 쓰시는 TTS의 실제 속도에 맞게 조절하세요. "
                             "(공백 제외 글자 수 ÷ 이 값 = 초)")
        st.slider("동시 생성 수 (병렬)", 1, 10, key="workers",
                  help="값이 크면 빠르지만 429(속도 제한)가 늘 수 있습니다. 429가 감지되면 자동으로 낮춥니다.")
        st.selectbox("ZIP 이미지 파일명 규칙", list(NAME_RULES), key="name_rule")
        st.checkbox("ZIP에 애니메이션 프롬프트(.txt)도 함께 넣기", key="zip_motion",
                    help="이미지와 같은 이름의 .txt 파일로 들어갑니다.")
        st.checkbox("일괄 생성에 애니메이션 프롬프트 포함", key="motion_auto")

    with st.expander(":material/folder: 프로젝트 저장 / 불러오기"):
        st.text_input("프로젝트 이름", key="project_name", help="projects/<이름>/ 폴더에 저장됩니다.")
        st.checkbox("자동 저장", key="autosave", help="단계가 끝날 때마다, 이미지가 만들어질 때마다 저장합니다.")
        c_s, c_l = st.columns(2)
        if c_s.button(":material/save: 지금 저장", width="stretch"):
            save_project(full=True)
            st.toast("저장했습니다.")
        st.selectbox("불러올 프로젝트", ["(선택)"] + list_projects(), key="load_choice")
        c_l.button(":material/folder_open: 불러오기", width="stretch", on_click=load_project)

api_key = ss.api_key
model_label, text_model, aspect = ss.model_label, ss.text_model, ss.aspect
model_id = ss[f"mid_{model_label}"]
sec_per_scene, cps, image_lang, workers = ss.sec, ss.cps, ss.lang, ss.workers

# ───────────────────────── 메인 입력 ─────────────────────────
st.title("대본 → 이미지 자동 생성기")
st.caption("대본을 붙여 넣고 ① 장면 분할 → ② 일괄 생성 순서로 누르세요. 세부 설정은 기본값으로 충분합니다.")

with st.expander(":material/description: .txt 파일로 대본 불러오기"):
    txt_file = st.file_uploader("대본 .txt", type=["txt"], label_visibility="collapsed")
    if txt_file is not None and ss.get("_txt_name") != (txt_file.name, txt_file.size):
        ss._txt_name = (txt_file.name, txt_file.size)
        try:
            ss.script = txt_file.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            ss.script = txt_file.getvalue().decode("cp949", errors="replace")

st.text_area("대본", height=260, key="script", placeholder="여기에 대본을 붙여 넣으세요.")
script = ss.script
chars, est_sec, est_n = estimate(script, sec_per_scene, cps)
if chars:
    est_min = est_n * SEC_PER_IMAGE_GUESS / max(1, workers) / 60
    st.caption(f"대본 {chars:,}자 → 장면 약 {est_n}개 · 이미지 {est_n}장, "
               f"텍스트 호출 약 {math.ceil(est_n / 15) + 1}회 · 이미지 생성 예상 약 {est_min:.1f}분 "
               f"(장당 {SEC_PER_IMAGE_GUESS}초 가정, 실제와 다를 수 있음)")

_c1, _c2, _c3 = st.columns([2, 3, 1])
run_split = _c1.button(":material/content_cut: 장면 분할", width="stretch",
                       help="대본을 장면으로 나눠 1번부터 차례로 보여 줍니다. (API 호출 없음)")
run_batch = _c2.button(":material/play_arrow: 일괄 생성", type="primary", width="stretch",
                       help="나뉜 장면을 기반으로 프롬프트 작성 → 이미지 생성까지 한 번에 진행합니다. "
                            "장면이 아직 안 나뉘어 있으면 먼저 나눕니다. 이미 만든 장면은 건너뜁니다.")
stop = _c3.button(":material/stop_circle: 중단", key="stop_btn", width="stretch",
                  help="실행 중인 작업을 멈춥니다. 이미 만든 이미지는 남고, 진행 중이던 장이 끝나면 멈춥니다.")

with st.expander(":material/palette: 스타일 · 규칙 설정 (기본값으로 충분해요)"):
    tab_style, tab_rule, tab_motion, tab_ref = st.tabs(["스타일", "장면 묘사 규칙", "움직임 규칙", "레퍼런스 이미지"])
    with tab_style:
        options = list(BUILTIN_PRESETS) + list(load_custom_presets()) + [CUSTOM_LABEL]
        if ss.preset not in options:
            ss.preset = CUSTOM_LABEL
        st.selectbox("스타일 프리셋", options, key="preset", on_change=on_preset_change)
        st.text_area("스타일 가이드 내용 (수정 가능)", height=140, key="style",
                     placeholder="예: A watercolor illustration ... The scene depicts")
        p1, p2, p3 = st.columns([2, 1, 1])
        p1.text_input("내 프리셋으로 저장할 이름", key="new_preset_name", label_visibility="collapsed",
                      placeholder="프리셋 이름 입력 후 저장")
        p2.button(":material/save: 저장", on_click=save_preset, width="stretch")
        p3.button(":material/delete: 삭제", on_click=delete_preset, width="stretch", disabled=ss.preset not in load_custom_presets())
    with tab_rule:
        st.text_area("형식 이미지 프롬프트 (장면 묘사가 따를 규칙)", height=150, key="format_prompt")
    with tab_motion:
        st.text_area("애니메이션 지시 규칙 (움직임 프롬프트가 따를 규칙)", height=120, key="motion_rule")
        st.text_input(":material/volume_off: 항상 붙일 무음 지시문 (대사·배경음악 없음)", key="silent_rule",
                      help="모든 애니메이션 프롬프트 끝에 자동으로 붙습니다. 표에서 지워도 내보낼 때 다시 붙습니다.")
    with tab_ref:
        ref_files = st.file_uploader(f"레퍼런스 이미지 (최대 {MAX_REFS}장, 인물·소품 일관성용)",
                                     type=["png", "jpg", "jpeg", "webp"], accept_multiple_files=True)
        if ref_files:
            ss.refs = [{"name": f.name, "data": f.getvalue(), "mime": f.type or "image/png"} for f in ref_files[:MAX_REFS]]
        if ss.refs:
            rc = st.columns(len(ss.refs))
            for c, r in zip(rc, ss.refs):
                c.image(r["data"], caption=r["name"][:12], width="stretch")
            if st.button(":material/close: 레퍼런스 비우기"):
                ss.refs = []
                st.rerun()
style, format_prompt, motion_rule = ss.style, ss.format_prompt, ss.motion_rule

_any_run = any([run_split, run_batch])
if _any_run:
    ss.log = []
if _any_run or ss.get("log"):
    _log_box = st.expander(":material/terminal: 진행 로그", expanded=_any_run).empty()
else:
    _log_box = None


def log(msg: str):
    """진행 상황을 번호 붙여 기록하고 로그 상자를 갱신한다."""
    ss.log = [*ss.get("log", []), msg]
    if _log_box is not None:
        _log_box.code("\n".join(f"{i:02d}  {m}" for i, m in enumerate(ss.log[-40:], 1)), language=None)


if _log_box is not None and ss.get("log") and not _any_run:
    _log_box.code("\n".join(f"{i:02d}  {m}" for i, m in enumerate(ss.log[-40:], 1)), language=None)


def need_key() -> bool:
    if not api_key:
        st.error("사이드바에 Gemini API 키를 입력하세요.")
        return False
    return True


def need_script() -> bool:
    if not script.strip():
        st.error("대본을 입력하세요.")
        return False
    return True


def autosave(full=False):
    if ss.autosave:
        save_project(full=full)


def do_analyze(force=True):
    h = script_hash(script)
    if not force and ss.analysis and ss.analysis_hash == h:
        return  # 같은 대본이면 분석 결과 재사용
    log(f"대본 분석 중 ({text_model})…")
    with st.spinner("1/4 대본 분석 중…"):
        ss.analysis = analyze_script(get_client(api_key), text_model, script)
    ss.analysis_hash = h
    autosave()


def do_split():
    log(f"본문을 분할하는 중… ({_nsp(script):,}자, 장면당 {sec_per_scene}초 목표)")
    scenes, total = split_script(script, sec_per_scene, cps)
    ss.scenes, ss.total_sec = scenes, total
    ss.split_sig = (script_hash(script), sec_per_scene, cps)
    log(f"총 {len(scenes)}개 장면으로 분할 완료")
    autosave(full=True)


def rewrite_prompts(only_ids=None):
    """only_ids=None이면 전체, 아니면 해당 번호 장면의 프롬프트만 다시 만든다."""
    targets = [s for s in ss.scenes if only_ids is None or s["idx"] in only_ids]
    if not targets:
        return
    log(f"장면별 프롬프트 작성 중 ({text_model}) · {len(targets)}장면")
    bar = st.progress(0.0, text="이미지 프롬프트 생성 중…")
    descs = build_scene_descriptions(
        get_client(api_key), text_model, ss.analysis or {}, targets, format_prompt, image_lang,
        progress_cb=lambda d, t: bar.progress(d / t, text=f"프롬프트 {d}/{t}"))
    for s in targets:
        desc = descs.get(s["idx"]) or s["text"]  # 누락 시 대본 원문으로 대체
        s["desc"] = desc
        s["prompt"] = compose_prompt(style, desc)
        s["psig"] = current_psig()
        for k in ("image", "error", "gen_prompt", "motion"):
            s.pop(k, None)
    bar.empty()
    autosave(full=True)


def do_motion(only_ids=None):
    targets = [s for s in ss.scenes if only_ids is None or s["idx"] in only_ids]
    if not targets:
        st.warning("장면이 없습니다. 2단계(분할)를 먼저 실행하세요.")
        return
    if not ss.analysis:
        do_analyze(force=False) if script.strip() else None
    log(f"움직임 프롬프트 작성 중 ({text_model}) · {len(targets)}장면")
    bar = st.progress(0.0, text="애니메이션 프롬프트 생성 중…")
    res = build_motion_prompts(get_client(api_key), text_model, ss.analysis, targets, motion_rule,
                               progress_cb=lambda d, t: bar.progress(d / t, text=f"애니메이션 프롬프트 {d}/{t}"))
    for s in targets:
        base = res.get(s["idx"]) or "Slow gentle zoom in, subtle ambient movement, keep the illustration style unchanged."
        s["motion"] = ensure_silent(base, ss.silent_rule)
    bar.empty()
    autosave()


def do_images(only=None, force=False):
    targets = [s for s in ss.scenes if s.get("prompt")
               and (s["idx"] in only if only is not None else (force or not s.get("image")))]
    if not targets:
        st.info("모든 장면의 이미지가 이미 만들어져 있습니다.")
        return
    by_idx = {s["idx"]: s for s in ss.scenes}
    for t in targets:
        t.pop("error", None)
        t["gen_prompt"] = compose_gen_prompt(t)
    log(f"이미지 생성 중 ({model_id}) · 대상 {len(targets)}장")
    log(f"병렬 처리 Worker: {workers}개")
    throttle = Throttle(workers)
    bar = st.progress(0.0, text=f"4/4 이미지 생성 0/{len(targets)} (동시 {workers}개)")
    holder = st.empty()
    slots = {}
    with holder.container():  # 장면 1..N 줄을 미리 만들어 두고, 끝나는 줄부터 채운다
        for sc in ss.scenes:
            slots[sc["idx"]] = st.empty()
            render_scene_row(slots[sc["idx"]], sc, busy=True)
    done = [0]

    def on_done(idx, img, err):
        done[0] += 1
        by_idx[idx]["image"] = img
        by_idx[idx]["error"] = err
        if img and ss.autosave:
            save_image(by_idx[idx])
        bar.progress(done[0] / len(targets),
                     text=f"4/4 이미지 생성 {done[0]}/{len(targets)} (현재 동시 {throttle.limit}개)")
        render_scene_row(slots[idx], by_idx[idx], busy=True)  # 이 장면의 카드만 갱신
    run_parallel(get_client(api_key), model_id, aspect, targets, workers, on_done, ss.refs, throttle)
    bar.empty()
    holder.empty()
    fails = [t for t in targets if not by_idx[t["idx"]].get("image")]
    log(f"이미지 생성 완료: 성공 {len(targets) - len(fails)} / 실패 {len(fails)}")
    (st.warning if fails else st.success)(f"완료: 성공 {len(targets) - len(fails)} / 실패 {len(fails)}")
    if throttle.cuts:
        st.warning(f"속도 제한(429)이 감지되어 동시 생성 수를 {workers}→{throttle.limit}개로 자동 낮췄습니다. "
                   "계속 실패하면 사이드바에서 동시 생성 수를 낮춰 보세요.")
    elif any("429" in (t.get("error") or "") or "RESOURCE_EXHAUSTED" in (t.get("error") or "") for t in fails):
        st.warning("속도 제한(429)으로 실패했습니다. 동시 생성 수를 낮추고 '일괄 생성'을 다시 눌러 보세요.")
    st.toast("이미지 생성 완료" if not fails else f"이미지 생성 완료 (실패 {len(fails)}장)")
    autosave()


def do_batch():
    """나뉜 장면을 기반으로 프롬프트 → (움직임 프롬프트) → 이미지를 만든다. 이미 된 장면은 건너뛴다."""
    if not ss.scenes or ss.get("split_sig") != (script_hash(script), sec_per_scene, cps):
        do_split()  # 아직 안 나눴거나, 대본·분할 설정이 바뀌었으면 먼저 나눈다
    do_analyze(force=False)
    psig = current_psig()
    # 프롬프트가 없거나, 지금 스타일·규칙과 다르게 만들어진 장면만 새로 만든다. (개별 생성한 장면은 그대로 둔다)
    stale = {sc["idx"] for sc in ss.scenes if not sc.get("prompt") or sc.get("psig") not in (None, psig)}
    if stale:
        rewrite_prompts(stale)
    if ss.motion_auto:
        need = {sc["idx"] for sc in ss.scenes if not sc.get("motion")}
        if need:
            do_motion(need)
    do_images()


try:
    if stop:
        st.info("⏹ 작업을 중단했습니다. 이미 만들어진 이미지는 그대로 남아 있어요. "
                "'일괄 생성'을 다시 누르면 남은 장면부터 이어서 만듭니다.")
    elif run_split and need_script():
        do_split()
    elif run_batch and need_key() and need_script():
        do_batch()
except Exception as e:  # noqa: BLE001
    st.error(f"오류: {e}")

# ───────────────────────── 결과 표시 ─────────────────────────
if ss.scenes:
    n_img = sum(1 for s in ss.scenes if s.get("image"))
    st.subheader(f"장면 {len(ss.scenes)}개 · 이미지 {n_img}장 완료")
    if n_img:  # 내려받을 이미지가 생긴 뒤에만 내보내기 버튼을 보여 준다
        e1, e2, e3 = st.columns(3)
        e1.download_button(":material/download: 이미지 ZIP",
                           make_zip(ss.scenes, ss.name_rule, ss.zip_motion, ss.silent_rule),
                           "images.zip", "application/zip", width="stretch")
        e2.download_button(":material/download: 장면/프롬프트 CSV", make_csv(ss.scenes, ss.silent_rule),
                           "scenes.csv", "text/csv", width="stretch")
        if any(s.get("motion") for s in ss.scenes):
            e3.download_button(":material/download: 애니메이션 프롬프트 TXT",
                               make_motion_txt(ss.scenes, ss.silent_rule), "motion_prompts.txt", "text/plain",
                               width="stretch")
    render_rows(st.container(), ss.scenes, interactive=True)
    autosave()
