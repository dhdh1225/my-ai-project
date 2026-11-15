#!/usr/bin/env python3
"""유튜브 업로드 자동화 CLI.

  python main.py upload queue.csv            # 목록의 영상을 모두 업로드
  python main.py upload queue.csv --dry-run  # 검증만
  python main.py stats                       # 내 채널 통계
  python main.py render projects/my_project  # 장면 이미지 → 완성 영상(mp4)
  python main.py render projects/my_project --test 1   # 1번 장면만 Grok 영상 테스트
"""
import os
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


def cmd_render(args):
    from video_auto.render import plan, render
    if args.test:
        args.ai = str(args.test)
    est = plan(args.project, args.ai, args.max_sec, args.price)
    print(f"장면 {est['scenes']}개 · 대본 기준 길이 약 {est['length'] / 60:.1f}분")
    print(f"AI 영상 장면 {len(est['ai_scenes'])}개 (요청 {est['ai_seconds']}초) "
          f"→ 예상 비용 약 ${est['est_cost']} (초당 ${args.price} 가정, 실제 요금은 xAI 콘솔 확인)")
    if est["missing_images"]:
        print(f"이미지 없는 장면: {est['missing_images'][:20]} — 앱에서 먼저 생성하세요.")
        return 1
    if est["ai_scenes"] and not args.yes:
        if input("진행할까요? [y/N] ").strip().lower() not in ("y", "yes"):
            print("취소했습니다.")
            return 0
    extra = {}
    if args.resolution:
        extra["resolution"] = args.resolution
    res = render(
        args.project, xai_key=os.environ.get("XAI_API_KEY", ""),
        gemini_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
        ai_spec=args.ai, grok_model=args.model, grok_max_sec=args.max_sec, grok_extra=extra or None,
        narration=args.narration, narration_file=args.narration_file, tts_voice=args.voice,
        bgm_file=args.bgm, bgm_volume=args.bgm_volume, height=args.height, workers=args.workers,
        only=[args.test] if args.test else None)
    if res["ai_failed"]:
        print("AI 영상 실패 → 줌효과로 대체한 장면:")
        for k, v in sorted(res["ai_failed"].items()):
            print(f"  #{k}: {v}")
    if args.test:
        print(f"테스트 클립: {res['clips'][0]}")
    else:
        print(f"완성: {res['output']} ({res['duration'] / 60:.1f}분, {res['size']})\n자막: {res['srt']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="유튜브 자동화")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("upload", help="목록 기반 일괄 업로드")
    up.add_argument("queue", help="queue.csv 또는 queue.json")
    up.add_argument("--dry-run", action="store_true")
    up.set_defaults(func=cmd_upload)
    st = sub.add_parser("stats", help="채널 통계")
    st.set_defaults(func=cmd_stats)
    rd = sub.add_parser("render", help="프로젝트 → 완성 영상")
    rd.add_argument("project", help="projects/<이름> 폴더")
    rd.add_argument("--ai", default="30%", help="AI 영상 장면: all / none / 30%% / 1,3,5-8 (기본 30%%)")
    rd.add_argument("--test", type=int, help="이 번호 장면 하나만 AI 영상으로 만들어 보고 끝냄")
    rd.add_argument("--model", default="grok-imagine-video", help="Grok 영상 모델 ID")
    rd.add_argument("--max-sec", type=int, default=10, help="AI 클립 최대 길이(초)")
    rd.add_argument("--resolution", help="Grok에 넘길 해상도 값(예: 720p). 비우면 기본값")
    rd.add_argument("--price", type=float, default=0.07, help="견적용 초당 가격($)")
    rd.add_argument("--narration", choices=["tts", "file", "none"], default="tts")
    rd.add_argument("--narration-file", help="--narration file 일 때 음성 파일")
    rd.add_argument("--voice", default="Kore", help="Gemini TTS 목소리 이름")
    rd.add_argument("--bgm", help="배경음악 파일(반복 재생)")
    rd.add_argument("--bgm-volume", type=float, default=0.12)
    rd.add_argument("--height", type=int, default=720, choices=[720, 1080])
    rd.add_argument("--workers", type=int, default=4, help="AI 영상 동시 요청 수")
    rd.add_argument("-y", "--yes", action="store_true", help="비용 확인 없이 진행")
    rd.set_defaults(func=cmd_render)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
