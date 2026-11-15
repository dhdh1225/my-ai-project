#!/usr/bin/env python3
"""유튜브 업로드 자동화 CLI.

  python main.py upload queue.csv            # 목록의 영상을 모두 업로드
  python main.py upload queue.csv --dry-run  # 검증만
  python main.py stats                       # 내 채널 통계
"""
import argparse
import sys
from pathlib import Path

from youtube_auto.queue import load_items, load_log, save_log


def cmd_upload(args):
    items = load_items(args.queue)
    log = load_log()
    base = Path(args.queue).parent

    todo = []
    for i, it in enumerate(items, 1):
        f = Path(it["file"])
        if not f.is_absolute():
            f = base / f
        it["file"] = str(f)
        if it.get("thumbnail") and not Path(it["thumbnail"]).is_absolute():
            it["thumbnail"] = str(base / it["thumbnail"])
        key = str(f)
        if key in log:
            print(f"[{i}] 건너뜀(이미 업로드됨): {it['title']} -> {log[key]}")
            continue
        if not f.exists():
            print(f"[{i}] 오류: 파일 없음 {f}")
            continue
        if not it.get("title"):
            print(f"[{i}] 오류: title 없음")
            continue
        todo.append(it)

    print(f"업로드 대상 {len(todo)}개")
    if args.dry_run or not todo:
        return

    from youtube_auto.auth import get_service
    from youtube_auto.uploader import upload_video
    service = get_service()
    for it in todo:
        print(f"업로드 중: {it['title']}")
        vid = upload_video(service, it)
        log[it["file"]] = f"https://youtu.be/{vid}"
        save_log(log)  # 건별 즉시 저장
        print(f"  완료: https://youtu.be/{vid}")


def cmd_stats(_):
    from youtube_auto.auth import get_service
    svc = get_service()
    ch = svc.channels().list(part="snippet,statistics", mine=True).execute()["items"][0]
    s = ch["statistics"]
    print(f"채널: {ch['snippet']['title']}")
    print(f"구독자 {s.get('subscriberCount', '숨김')} · 조회수 {s['viewCount']} · 영상 {s['videoCount']}")


def main():
    ap = argparse.ArgumentParser(description="유튜브 자동화")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="목록 기반 일괄 업로드")
    up.add_argument("queue", help="queue.csv 또는 queue.json")
    up.add_argument("--dry-run", action="store_true")
    up.set_defaults(func=cmd_upload)
    st = sub.add_parser("stats", help="채널 통계")
    st.set_defaults(func=cmd_stats)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
