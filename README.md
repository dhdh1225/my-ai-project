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
pip install -r requirements.txt
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
pip install -r requirements-app.txt
streamlit run app.py
```

- 이미지 모델: 나노바나나 프로 / 나노바나나2 (모델 ID는 사이드바에서 수정 가능)
- 장면당 길이 3~20초 선택, 낭독 속도(글자/초)로 전체 길이 추정
- 형식 이미지 프롬프트 + 스타일 가이드(기본 "지구박물관", 직접 입력 가능), 장면별 프롬프트 수정 가능
- 병렬 생성(동시 1~10개), 429 등은 지수 백오프 재시도, 실패 장면만 재생성
- 결과 ZIP / CSV 다운로드
