# my-ai-project — 유튜브 업로드 자동화

CSV/JSON 목록에 적어 둔 영상을 YouTube Data API로 일괄 업로드합니다.
제목·설명·태그·카테고리·썸네일·예약 발행을 지원하고, 업로드 기록(`upload_log.json`)으로 중복 업로드를 막습니다.

## API 설정 (최초 1회)
1. [Google Cloud Console](https://console.cloud.google.com)에서 프로젝트 생성
2. **YouTube Data API v3** 사용 설정
3. OAuth 동의 화면 구성 후 테스트 사용자에 본인 계정 추가
4. 사용자 인증 정보 → **OAuth 클라이언트 ID (데스크톱 앱)** 생성 → JSON을 `client_secret.json`으로 저장
5. 첫 실행 때 브라우저 로그인 → `token.json` 자동 생성

## 사용법
```bash
pip install -r requirements-youtube.txt
cp queue.example.csv queue.csv   # 내용 수정
python main.py upload queue.csv --dry-run   # 검증
python main.py upload queue.csv             # 실제 업로드
python main.py stats                        # 채널 통계
```

### 목록 컬럼
| 컬럼 | 설명 |
|---|---|
| file | 영상 경로(목록 파일 기준 상대경로 가능) |
| title | 제목(최대 100자) |
| description / tags | 설명 / 쉼표로 구분한 태그 |
| category | 카테고리 ID (기본 22) |
| privacy | public / unlisted / private (기본 private) |
| publish_at | 예약 발행 시각 `YYYY-MM-DD HH:MM` (지정 시 private로 업로드 후 자동 공개) |
| thumbnail | 썸네일 이미지 경로 |

## 주의
- `client_secret.json`, `token.json`은 절대 커밋하지 마세요(.gitignore 처리됨).
- 인증 전 API 프로젝트는 업로드 영상이 비공개로 잠길 수 있으며, 일일 쿼터(업로드 1건≈1,600 유닛, 기본 10,000)가 있습니다.

---

# 대본 → 이미지 자동 생성기 (`app.py`)

Streamlit 단일 파일 앱. 대본을 넣으면 **대본 분석 → 초 단위 분할 → 이미지 프롬프트화 → 이미지 병렬 생성**을 자동으로 진행합니다 (Gemini API).

```bash
pip install -r requirements.txt
streamlit run app.py
```

- 이미지 모델: 나노바나나 프로 / 나노바나나2 (모델 ID는 사이드바에서 수정 가능)
- 장면당 길이 3·4·5·6·10·15·20초 중 선택, 낭독 속도(글자/초)로 전체 길이 추정
- 이미지 속 글자 언어: 한국어 / 일본어 / 영어 / 언어 없음(기본, 글자·자막·로고 금지)
- 형식 이미지 프롬프트 + 스타일 가이드(기본 "지구박물관"), **내 스타일 프리셋 저장/삭제**(`presets.json`)
- 병렬 생성(동시 1~10개), 429 감지 시 **동시 생성 수 자동 감속**, 지수 백오프 재시도
- **프로젝트 자동 저장/불러오기** (`projects/<이름>/`: 설정·프롬프트·이미지·레퍼런스, API 키는 저장 안 함)
- **이어서 생성**(이미 만든 이미지는 건너뜀), 실패 장면 전부 재생성, 선택 장면 재생성
- **테스트 1장**만 먼저 생성, 실행 전 장면 수·호출 수·예상 시간 표시
- **레퍼런스 이미지**(최대 5장)로 인물·소품 외형 일관성 유지
- 장면 **합치기/나누기**, 대본·프롬프트 표 직접 수정, 선택 장면 프롬프트만 재작성
- 같은 대본이면 분석 결과 재사용, `.txt` 대본 업로드, 환경변수 `GEMINI_API_KEY` 자동 입력
- **장면별 애니메이션(이미지-투-비디오) 프롬프트** 생성: 장면 길이에 맞는 움직임 지시문, 규칙 직접 수정 가능
- 애니메이션 프롬프트 끝에 **무음 지시문(대사·배경음악·효과음 없음)** 자동 부착 (직접 수정 가능, 내보낼 때도 보장)
- **ZIP 파일명 규칙 선택**(001_00m05s / scene_001 / 001 / 001_5s), 이미지와 같은 이름의 .txt로 움직임 프롬프트 동봉 가능
- 결과 ZIP / CSV / **SRT(타임코드 자막)** / 애니메이션 프롬프트 TXT 다운로드

## 화면 테마
흰색 + 웜그레이 바탕에 살구색 포인트를 섞은 테마입니다. 색은 `app.py` 상단의 `THEME_CSS`(`:root` 색 변수)에서, 기본 밝은 테마는 `.streamlit/config.toml`에서 바꿀 수 있습니다. (config 파일이 없어도 `app.py`만으로 동작합니다.)

## 배포 (Streamlit Community Cloud, 폰에서도 사용)
1. [share.streamlit.io](https://share.streamlit.io) → **Create app** → 이 GitHub 저장소 / 브랜치 `main` / 메인 파일 `app.py`
2. **Advanced settings → Secrets**에 아래를 입력 (코드나 저장소에는 키를 넣지 마세요)
   ```toml
   GEMINI_API_KEY = "내 Gemini API 키"
   APP_PASSWORD = "접속 비밀번호"
   ```
3. 배포 후 주소를 폰 브라우저로 열고, 홈 화면에 추가하면 앱처럼 쓸 수 있습니다.

주의
- `APP_PASSWORD`를 설정하지 않으면 주소를 아는 누구나 내 API 키로 이미지를 만들 수 있습니다. 반드시 설정하세요.
- 배포 서버는 재시작되면 `projects/`, `presets.json`이 사라질 수 있습니다. 생성 후 ZIP/CSV를 바로 내려받으세요.
- 유튜브 업로더(`main.py`)용 패키지는 `requirements-youtube.txt`로 분리되어 있습니다.

---

# 장면 이미지 → 완성 영상 자동화 (`video_auto/`)

앱에서 만든 프로젝트(이미지·움직임 프롬프트·대본)를 **완성 mp4**로 만듭니다.

1. **내레이션**: Gemini TTS(장면별 낭독, 실제 길이에 맞춰 장면·자막 시간 자동 조정) / 내 음성 파일 / 없음
2. **장면 클립**: 고른 장면은 **Grok Imagine AI 영상**(이미지+움직임 프롬프트), 나머지는 **무료 줌·패닝 효과**
   - AI 클립이 장면보다 짧으면 천천히 재생(최대 1.5배) 후 마지막 장면에 줌효과를 이어 채움
   - AI 생성이 실패한 장면은 자동으로 줌효과로 대체 (영상이 비지 않음)
3. **합치기**: 클립 연결 + 내레이션 + 배경음악(반복, 볼륨 조절) → `projects/<이름>/output/final.mp4` + 실제 타이밍 `final.srt`

중간 결과(`clips/`, `tts/`)는 캐시되어, 중단 후 다시 실행하면 이미 만든 장면은 다시 요청하지 않습니다(비용 중복 없음).

## 사용법
앱 맨 아래 **🎬 6. 영상 만들기**에서 버튼으로 실행하거나, PC에서 명령어로 실행합니다.

```bash
export XAI_API_KEY=...      # xAI 콘솔(console.x.ai)에서 발급
export GEMINI_API_KEY=...   # TTS용
python main.py render projects/my_project --test 1        # 1번 장면만 AI 영상 테스트 (먼저 꼭!)
python main.py render projects/my_project --ai 30%        # 30%만 AI, 나머지 줌효과
python main.py render projects/my_project --ai none       # 전부 줌효과 (무료)
python main.py render projects/my_project --ai 1,5,10-12 --bgm bgm.mp3
python main.py render projects/my_project --narration file --narration-file voice.mp3
```

- 실행 전에 AI 장면 수와 **예상 비용**을 보여 주고 확인을 받습니다(`-y`로 생략).
- `--model`로 Grok 모델 ID(기본 `grok-imagine-video`), `--max-sec`로 AI 클립 최대 길이를 바꿀 수 있습니다.
- ffmpeg이 필요합니다(윈도우 `winget install ffmpeg`, 맥 `brew install ffmpeg`). Streamlit Cloud는 `packages.txt`로 자동 설치됩니다.
- Streamlit Cloud Secrets에 `XAI_API_KEY = "..."`를 넣으면 앱에 자동 입력됩니다.
- 클라우드 서버는 CPU·메모리가 작아 긴 영상은 느릴 수 있고, 폰 화면이 꺼지면 작업이 끊길 수 있습니다. 롱폼은 PC 명령어 실행을 권장합니다.
