"""대본 → 이미지 자동 생성기 (Streamlit 단일 파일)

파이프라인: 1) 대본 분석 → 2) 초 단위 분할 → 3) 이미지 프롬프트화 → 4) 이미지 병렬 생성
실행: streamlit run app.py
필요: pip install streamlit google-genai
"""
import csv
import io
import json
import math
import random
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

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

JIGU_STYLE = (
    "A clean, fully realized 2D vector illustration suitable for an educational "
    "history documentary. The style uses consistent bold black outlines and flat, "
    "muted colors with minimal shading. The composition is visually dense and "
    "multilayered, with the background fully populated by environmental elements "
    "appropriate to the specific time period and setting described. No empty "
    "negative space. The scene depicts"
)
STYLE_PRESETS = {"지구박물관 (기본)": JIGU_STYLE, "직접 입력": ""}

DEFAULT_FORMAT_PROMPT = (
    "각 장면 묘사는 영어 한 문단(2~3문장)으로, 아래 순서를 따른다.\n"
    "[시대/장소] → [중심 인물과 행동] → [주변 소품·건축·배경 요소] → [구도(카메라 거리/각도)]\n"
    "- 같은 인물/장소는 전 장면에서 같은 외형·복장으로 일관되게 묘사한다.\n"
    "- 대본에 없는 사실을 지어내지 않는다."
)

PRIVATE_ERR_HINTS = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL", "timed out", "timeout")


# ───────────────────────── 유틸 ─────────────────────────
def lang_directive(lang: str) -> str:
    """이미지 속 글자 언어 규칙 (영문 지시문)."""
    name = IMAGE_LANGS.get(lang)
    if name is None:
        return ("The image must contain no text of any kind: no letters, numbers, captions, "
                "signs with writing, logos, watermarks or speech bubbles.")
    return (f"Any text that appears in the image (signs, labels, inscriptions, captions) must be "
            f"written in {name} only. Keep text minimal, short and spelled correctly.")


def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


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
def _units(text: str, max_len: int):
    """문장 단위로 쪼개되, 너무 긴 문장은 쉼표/공백 기준으로 추가 분할."""
    sents = [s.strip() for s in re.split(r"(?<=[.!?。…])\s+|\n+", text) if s.strip()]
    out = []
    for s in sents:
        if len(s) <= max_len:
            out.append(s)
            continue
        buf = ""
        for tok in re.split(r"(?<=[,，、])\s*|\s+", s):
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


def split_script(script: str, sec_per_scene: int, chars_per_sec: float):
    """글자 수/낭독 속도로 전체 길이를 추정해 장면당 sec_per_scene초가 되도록 분할."""
    text = script.strip()
    total_chars = len(re.sub(r"\s", "", text))
    if total_chars == 0:
        return [], 0.0
    total_sec = total_chars / chars_per_sec
    n = max(1, math.ceil(total_sec / sec_per_scene))
    target = total_chars / n  # 장면당 목표 글자 수 (공백 제외)

    units = _units(text, max(10, int(target * 1.6)))
    groups = [[] for _ in range(n)]
    cum = 0
    for u in units:
        ln = len(re.sub(r"\s", "", u))
        mid = cum + ln / 2
        groups[min(n - 1, int(mid / target))].append(u)
        cum += ln
    groups = [g for g in groups if g]

    scenes, t = [], 0.0
    for i, g in enumerate(groups, 1):
        body = " ".join(g)
        dur = len(re.sub(r"\s", "", body)) / chars_per_sec
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


# ───────────────────────── 4단계: 이미지 병렬 생성 ─────────────────────────
def generate_one(client, model: str, prompt: str, aspect: str):
    """워커 스레드에서 실행 (Streamlit 호출 금지). (bytes|None, error|None) 반환."""
    from google.genai import types
    last = ""
    for attempt in range(5):
        try:
            res = client.models.generate_content(
                model=model,
                contents=[prompt],
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
            reason = ""
            if res.candidates:
                reason = str(getattr(res.candidates[0], "finish_reason", ""))
            last = f"이미지가 반환되지 않음 (안전 필터 등) {reason}".strip()
            if attempt >= 1:  # 필터 거절은 1회만 재시도
                break
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if not any(h.lower() in last.lower() for h in PRIVATE_ERR_HINTS):
                break  # 재시도해도 소용없는 오류(키 오류 등)
        time.sleep(min(30, 2 ** attempt) + random.random())  # 지수 백오프
    return None, last[:300]


def run_parallel(client, model, aspect, targets, workers, on_done):
    """targets: [scene dict]. 완료되는 순서대로 on_done(scene_idx, bytes, err) 호출 (메인 스레드)."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(generate_one, client, model, s.get("gen_prompt") or s["prompt"], aspect): s["idx"] for s in targets}
        for f in as_completed(futs):
            idx = futs[f]
            try:
                img, err = f.result()
            except Exception as e:  # noqa: BLE001
                img, err = None, str(e)[:300]
            on_done(idx, img, err)


# ───────────────────────── 화면 보조 ─────────────────────────
def render_grid(container, scenes, cols=4):
    with container.container():
        for r in range(0, len(scenes), cols):
            row = st.columns(cols)
            for c, s in zip(row, scenes[r:r + cols]):
                with c:
                    cap = f'#{s["idx"]} {fmt_time(s["start"])}~{fmt_time(s["end"])}'
                    if s.get("image"):
                        try:
                            st.image(s["image"], caption=cap, width="stretch")
                        except Exception:  # noqa: BLE001 - 깨진 이미지 하나로 화면이 죽지 않게
                            st.error(f"{cap}\n이미지를 표시할 수 없음")
                    elif s.get("error"):
                        st.error(f"{cap}\n실패: {s['error'][:80]}")
                    else:
                        st.info(f"{cap}\n대기/생성 중…")


def make_zip(scenes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for s in scenes:
            if s.get("image"):
                z.writestr(f'{s["idx"]:03d}_{fmt_time(s["start"]).replace(":", "m")}s.png', s["image"])
    return buf.getvalue()


def make_csv(scenes) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["번호", "시작", "끝", "대본", "프롬프트", "상태"])
    for s in scenes:
        status = "성공" if s.get("image") else ("실패: " + s["error"] if s.get("error") else "미생성")
        w.writerow([s["idx"], fmt_time(s["start"]), fmt_time(s["end"]), s["text"], s.get("gen_prompt") or s.get("prompt", ""), status])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# ───────────────────────── 앱 ─────────────────────────
st.set_page_config(page_title="대본 → 이미지 생성기", page_icon="🖼️", layout="wide")
ss = st.session_state
ss.setdefault("analysis", None)
ss.setdefault("scenes", [])
ss.setdefault("total_sec", 0.0)

st.title("🖼️ 대본 → 이미지 자동 생성기")
st.caption("대본분석 → 초 단위 분할 → 이미지 프롬프트화 → 이미지 병렬 생성 (Gemini API)")

with st.sidebar:
    st.header("⚙️ 설정")
    api_key = st.text_input("Gemini API 키", type="password", help="저장되지 않으며 이 세션에서만 사용됩니다.")
    model_label = st.selectbox("이미지 모델", list(IMAGE_MODELS))
    model_id = st.text_input("이미지 모델 ID", value=IMAGE_MODELS[model_label], key=f"mid_{model_label}",
                             help="모델 ID가 바뀌면 여기서 수정하세요.")
    text_model = st.text_input("분석·프롬프트화 텍스트 모델", value=DEFAULT_TEXT_MODEL)
    aspect = st.selectbox("화면 비율", ASPECT_RATIOS)
    sec_per_scene = st.selectbox("장면당 길이 (초 단위 분할)", SCENE_SECONDS, index=2,
                                 format_func=lambda x: f"{x}초", help="이미지 1장이 커버하는 시간")
    image_lang = st.selectbox("이미지 속 글자 언어", list(IMAGE_LANGS),
                              help="이미지에 들어가는 글자의 언어. '언어 없음'은 글자를 넣지 않습니다.")
    cps = st.number_input("낭독 속도 (글자/초)", 3.0, 15.0, 7.0, 0.5,
                          help="공백 제외 글자 수로 영상 길이를 추정합니다.")
    workers = st.slider("동시 생성 수 (병렬)", 1, 10, 4, 1,
                        help="값이 크면 빠르지만 429(속도 제한)가 늘 수 있습니다.")

col_a, col_b = st.columns([3, 2])
with col_a:
    script = st.text_area("📜 대본", height=300, placeholder="여기에 대본을 붙여 넣으세요.")
with col_b:
    format_prompt = st.text_area("🧩 형식 이미지 프롬프트 (장면 묘사가 따를 규칙)", value=DEFAULT_FORMAT_PROMPT, height=170)
    preset = st.selectbox("🎨 스타일 가이드", list(STYLE_PRESETS))
    if preset == "직접 입력":
        style = st.text_area("스타일 가이드 직접 입력", height=120,
                             placeholder="예: A watercolor illustration ... The scene depicts")
    else:
        style = st.text_area("스타일 가이드 (수정 가능)", value=STYLE_PRESETS[preset], height=170, key=f"style_{preset}")

b1, b2, b3, b4, b5 = st.columns(5)
run_all = b1.button("🚀 전체 자동 실행", type="primary", width="stretch")
run1 = b2.button("1 대본 분석", width="stretch")
run2 = b3.button("2 초 단위 분할", width="stretch")
run3 = b4.button("3 프롬프트화", width="stretch")
run4 = b5.button("4 이미지 생성", width="stretch")


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


def do_analyze():
    with st.spinner("1/4 대본 분석 중…"):
        ss.analysis = analyze_script(get_client(api_key), text_model, script)
    ss.scenes = []  # 이후 단계 초기화


def do_split():
    scenes, total = split_script(script, sec_per_scene, cps)
    ss.scenes, ss.total_sec = scenes, total


def do_prompts():
    if not ss.scenes:
        do_split()
    if not ss.analysis:
        do_analyze()
    bar = st.progress(0.0, text="3/4 이미지 프롬프트 생성 중…")
    descs = build_scene_descriptions(
        get_client(api_key), text_model, ss.analysis, ss.scenes, format_prompt, image_lang,
        progress_cb=lambda d, t: bar.progress(d / t, text=f"3/4 프롬프트 {d}/{t}"))
    for s in ss.scenes:
        desc = descs.get(s["idx"]) or s["text"]  # 누락 시 대본 원문으로 대체
        s["desc"] = desc
        s["prompt"] = compose_prompt(style, desc)
        s.pop("image", None)
        s.pop("error", None)
    bar.empty()


def do_images(only=None):
    targets = [s for s in ss.scenes if s.get("prompt") and (only is None or s["idx"] in only)]
    if not targets:
        st.warning("생성할 장면(프롬프트)이 없습니다. 3단계를 먼저 실행하세요.")
        return
    by_idx = {s["idx"]: s for s in ss.scenes}
    directive = lang_directive(image_lang)
    for t in targets:
        t.pop("error", None)
        t["gen_prompt"] = f'{t["prompt"].strip()} {directive}'
    holder = st.empty()
    bar = st.progress(0.0, text=f"4/4 이미지 생성 0/{len(targets)} (동시 {workers}개)")
    done = [0]

    def on_done(idx, img, err):
        done[0] += 1
        by_idx[idx]["image"] = img
        by_idx[idx]["error"] = err
        bar.progress(done[0] / len(targets), text=f"4/4 이미지 생성 {done[0]}/{len(targets)}")
        render_grid(holder, ss.scenes)

    render_grid(holder, ss.scenes)
    run_parallel(get_client(api_key), model_id, aspect, targets, workers, on_done)
    bar.empty()
    fails = sum(1 for t in targets if not by_idx[t["idx"]].get("image"))
    (st.warning if fails else st.success)(f"완료: 성공 {len(targets) - fails} / 실패 {fails}")


try:
    if run_all and need_key() and need_script():
        do_analyze()
        do_split()
        do_prompts()
        do_images()
    elif run1 and need_key() and need_script():
        do_analyze()
    elif run2 and need_script():
        do_split()
    elif run3 and need_key() and need_script():
        do_prompts()
    elif run4 and need_key():
        do_images()
except Exception as e:  # noqa: BLE001
    st.error(f"오류: {e}")

# ───────────────────────── 결과 표시 ─────────────────────────
if ss.analysis:
    with st.expander("📊 1. 대본 분석 결과", expanded=False):
        st.json(ss.analysis)

if ss.scenes:
    st.subheader(f"🎬 장면 {len(ss.scenes)}개 · 추정 길이 {fmt_time(ss.total_sec)} · 장면당 {sec_per_scene}초")
    has_prompt = any(s.get("prompt") for s in ss.scenes)
    rows = [{"번호": s["idx"], "시간": f'{fmt_time(s["start"])}~{fmt_time(s["end"])}',
             "대본": s["text"], "프롬프트(수정 가능)": s.get("prompt", "")} for s in ss.scenes]
    edited = st.data_editor(rows, width="stretch", hide_index=True, disabled=["번호", "시간", "대본"],
                            key="editor", height=min(500, 60 + 38 * len(rows)))
    for s, row in zip(ss.scenes, edited):  # 편집 내용을 상태에 반영
        s["prompt"] = row["프롬프트(수정 가능)"]

    if any(s.get("image") or s.get("error") for s in ss.scenes):
        st.subheader("🖼️ 생성 결과")
        render_grid(st.container(), ss.scenes)
        bad = [s["idx"] for s in ss.scenes if not s.get("image") and s.get("prompt")]
        pick = st.multiselect("다시 생성할 장면 번호", [s["idx"] for s in ss.scenes], default=bad)
        if st.button("♻️ 선택 장면 재생성") and need_key():
            try:
                do_images(only=set(pick))
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"오류: {e}")
        d1, d2 = st.columns(2)
        if any(s.get("image") for s in ss.scenes):
            d1.download_button("⬇️ 이미지 ZIP", make_zip(ss.scenes), "images.zip", "application/zip",
                               width="stretch")
        d2.download_button("⬇️ 장면/프롬프트 CSV", make_csv(ss.scenes), "scenes.csv", "text/csv",
                           width="stretch")
