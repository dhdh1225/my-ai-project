"""xAI Grok Imagine 이미지-투-비디오 클라이언트 (제출 → 상태 조회 → 다운로드)."""
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.x.ai/v1"
DEFAULT_MODEL = "grok-imagine-video"
UA = "video-auto/1.0"
RETRY_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class GrokError(RuntimeError):
    pass


class GrokVideo:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, base_url: str = API_BASE,
                 extra: dict | None = None, timeout: float = 900, poll_sec: float = 5):
        if not api_key:
            raise GrokError("xAI API 키가 없습니다. (환경변수 XAI_API_KEY)")
        self.key, self.model, self.base = api_key, model, base_url.rstrip("/")
        self.extra, self.timeout, self.poll_sec = extra or {}, timeout, poll_sec

    def _req(self, method, url, body=None, tries=5):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:500]
                if e.code in RETRY_CODES and attempt < tries - 1:
                    time.sleep(min(60, 2 ** attempt * 3))
                    continue
                raise GrokError(f"HTTP {e.code}: {msg}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < tries - 1:
                    time.sleep(min(60, 2 ** attempt * 3))
                    continue
                raise GrokError(f"네트워크 오류: {e}") from None

    @staticmethod
    def _video_url(res: dict):
        v = res.get("video")
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
        if isinstance(v, str) and v.startswith("http"):
            return v
        for k in ("url", "video_url"):
            if res.get(k):
                return res[k]
        data = res.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("url"):
            return data[0]["url"]
        return None

    def generate(self, image_path, prompt: str, duration: int, out_path) -> Path:
        img = Path(image_path)
        mime = "image/jpeg" if img.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        uri = f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"
        body = {"model": self.model, "prompt": prompt, "image": {"url": uri}, "duration": int(duration), **self.extra}
        res = self._req("POST", f"{self.base}/videos/generations", body)
        url = self._video_url(res)
        rid = res.get("request_id") or res.get("id")
        t0 = time.time()
        while not url:
            if not rid:
                raise GrokError(f"응답에 request_id가 없습니다: {str(res)[:300]}")
            if time.time() - t0 > self.timeout:
                raise GrokError(f"시간 초과 ({int(self.timeout)}초)")
            time.sleep(self.poll_sec)
            res = self._req("GET", f"{self.base}/videos/{rid}")
            status = str(res.get("status", "")).lower()
            if status in ("failed", "error", "expired", "cancelled", "canceled", "rejected"):
                raise GrokError(f"생성 실패({status}): {str(res.get('error') or res)[:300]}")
            url = self._video_url(res) if status in ("done", "completed", "succeeded", "success", "") else None
        out = Path(out_path)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=300) as r:
            out.write_bytes(r.read())
        return out
