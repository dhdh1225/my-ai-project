"""영상 업로드, 썸네일 설정, 예약 발행."""
import time
from datetime import datetime, timezone

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

RETRIABLE = {500, 502, 503, 504}


def _to_rfc3339(s):
    """'2026-12-01 18:00' (로컬시간) 또는 ISO 문자열 -> UTC RFC3339."""
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()  # 로컬 시간대로 간주
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upload_video(service, item):
    """item: dict(file, title, description, tags, category, privacy, publish_at, thumbnail)"""
    privacy = item.get("privacy") or "private"
    status = {"privacyStatus": privacy,
              "selfDeclaredMadeForKids": str(item.get("made_for_kids", "")).lower() == "true"}
    if item.get("publish_at"):
        status["privacyStatus"] = "private"  # 예약 발행은 private 필수
        status["publishAt"] = _to_rfc3339(item["publish_at"])

    body = {
        "snippet": {
            "title": item["title"][:100],
            "description": item.get("description", ""),
            "tags": [t.strip() for t in str(item.get("tags", "")).split(",") if t.strip()],
            "categoryId": str(item.get("category") or "22"),
        },
        "status": status,
    }
    media = MediaFileUpload(item["file"], chunksize=8 * 1024 * 1024, resumable=True)
    req = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response, retry = None, 0
    while response is None:
        try:
            st, response = req.next_chunk()
            if st:
                print(f"  업로드 {int(st.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in RETRIABLE and retry < 5:
                retry += 1
                time.sleep(2 ** retry)
            else:
                raise
    video_id = response["id"]

    if item.get("thumbnail"):
        try:
            service.thumbnails().set(
                videoId=video_id, media_body=MediaFileUpload(item["thumbnail"])).execute()
        except HttpError as e:
            print(f"  썸네일 설정 실패(채널 인증 필요할 수 있음): {e}")
    return video_id
