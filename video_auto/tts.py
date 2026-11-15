"""Gemini TTS로 장면별 내레이션 생성 (24kHz 16bit 모노 PCM)."""
import time
import wave

DEFAULT_TTS_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_VOICE = "Kore"
DEFAULT_STYLE = "Read the following Korean narration in a calm, clear documentary tone:"
RATE = 24000


def synth(client, text: str, model: str = DEFAULT_TTS_MODEL, voice: str = DEFAULT_VOICE,
          style: str = DEFAULT_STYLE, tries: int = 5) -> bytes:
    from google.genai import types
    contents = f"{style}\n{text}" if style else text
    cfg = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))
    err = None
    for attempt in range(tries):
        try:
            res = client.models.generate_content(model=model, contents=contents, config=cfg)
            for part in res.candidates[0].content.parts:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
            raise RuntimeError("TTS 응답에 오디오가 없습니다.")
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(min(60, 2 ** attempt * 3))
    raise RuntimeError(f"TTS 실패: {err}")


def write_wav(path, pcm: bytes):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def read_wav(path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / 2 / RATE


def build_track(path, segments):
    """segments: [(시작초, pcm), ...] → 전체 길이 하나의 wav (사이는 무음)."""
    out = bytearray()
    for start, pcm in segments:
        pos = int(round(start * RATE)) * 2
        if pos > len(out):
            out.extend(b"\x00" * (pos - len(out)))
        out[pos:pos + len(pcm)] = pcm
    write_wav(path, bytes(out))
