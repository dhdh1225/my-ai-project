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

Streamlit 단일 파일 앱 (Gemini API). 사용 순서는 두 단계입니다.

1. 대본을 붙여 넣고 **장면 분할**: 대본이 장면으로 나뉘어 1번부터 카드로 나옵니다. (API 호출 없음)
2. **일괄 생성**: 나뉜 장면을 기반으로 프롬프트 작성 → 이미지 생성까지 알아서 진행합니다.

```bash
pip install -r requirements.txt
streamlit run app.py
```

3. 원하면 **카드마다 '이 장면 생성' 버튼**으로 한 장면만 따로 만들 수 있습니다. (그 장면의 프롬프트도 그 자리에서 작성) 일괄 생성은 이미 만든 장면을 건드리지 않고 나머지만 만듭니다.

- 장면은 **문장이 끝나는 곳(마침표 `. ! ? …`)에서만** 나뉩니다. 문장이 길면 그 장면이 목표 초보다 조금 길어질 뿐, 문장 중간에서는 끊지 않습니다.
- 이미지 모델: 나노바나나 프로 / 나노바나나2 (모델 ID는 사이드바 고급 설정에서 수정)
- 장면당 길이 3·4·5·6·10·15·20초 중 선택. 분량은 '낭독 속도(글자/초)'로 계산하니 쓰는 TTS 속도에 맞게 조절하세요.
- 이미지 속 글자 언어: 한국어 / 일본어 / 영어 / 언어 없음(기본, 글자·자막·로고 금지)
- 스타일 프리셋(기본 "지구박물관", 직접 저장 가능), 형식 프롬프트, 레퍼런스 이미지(인물·소품 일관성)
- 장면 카드: 대본 / 이미지 프롬프트 / 애니메이션 프롬프트(항상 무음 지시 포함), 장면별 다운로드·다시 만들기
- 병렬 생성, 429 자동 감속, 중단 버튼, 일괄 생성을 다시 누르면 이미 만든 장면은 건너뛰고 이어서 생성
- 스타일·규칙을 바꾸고 일괄 생성을 누르면 전체를 새로 만들고, 대본을 바꾸면 새로 나눕니다.
- 프로젝트 자동 저장/불러오기, 결과 ZIP / CSV / 애니메이션 프롬프트 TXT 내보내기

## 화면 테마
흰색 + 웜그레이 바탕에 살구색 포인트 테마입니다. 색은 `app.py` 상단의 `THEME_CSS`(`:root` 색 변수)에서 바꿀 수 있습니다.

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

## 모델이 단종되었을 때
- 텍스트 모델 기본값은 `gemini-3.8-flash`입니다. (이전 기본값 `gemini-2.5-flash` 단종 시 Google 오류가 권장한 모델)
- 모델이 없어졌다는 오류(404)가 나면 이 API 키로 쓸 수 있는 모델을 조회해 같은 계열의 대체 모델로 **자동 전환**합니다. (텍스트·이미지 모두, 이미지는 프로/플래시 같은 계열을 먼저 찾습니다.)
- 사이드바 **고급 설정 → 사용 가능한 모델 보기**에서 이 키로 실제 호출 가능한 모델 이름을 확인해 직접 입력할 수도 있습니다.
