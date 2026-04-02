# 🏛️ 지자체 공고 자동 수집 파이프라인

> **소상공인·자영업자·중소기업** 대상 지원금/보조금 공고를 자동으로 수집하여  
> AI로 정제한 뒤 `grants_data.json`으로 매일 자동 배포하는 무인 파이프라인.

---

## 📐 아키텍처 개요

```
관악구청 게시판 (고시/공고)
       │
       ▼
 scraper.py (BeautifulSoup)
       │  게시물 목록 수집
       ▼
 첨부파일 다운로드 (HWP / PDF)
       │
   ┌───┴───┐
   ▼       ▼
pdfplumber olefile / hwp5txt
   └───┬───┘
       │  순수 텍스트 추출
       ▼
 Gemini 1.5 Flash API
       │  지원금 공고 판별 + JSON 구조화
       ▼
 grants_data.json  ──▶  GitHub 자동 커밋 (매일 08:00 KST)
```

---

## 📁 파일 구조

```
local-grant-scraper/
├── scraper.py                    # 핵심 파이프라인 스크립트
├── requirements.txt              # Python 의존성
├── grants_data.json              # 수집 결과 (자동 생성)
├── scraper.log                   # 실행 로그 (자동 생성)
├── README.md                     # 이 파일
└── .github/
    └── workflows/
        └── scraper.yml           # GitHub Actions CI/CD
```

---

## ⚡ 로컬 테스트 실행

### 1. 사전 준비

Python 3.10 이상이 설치되어 있어야 합니다.

```bash
python --version  # 3.10+ 확인
```

### 2. 가상환경 생성 및 활성화

```bash
# 가상환경 생성
python -m venv venv

# 활성화 (Windows)
venv\Scripts\activate

# 활성화 (macOS / Linux)
source venv/bin/activate
```

### 3. 의존성 설치

```bash
pip install -r requirements.txt
```

### 4. Gemini API 키 환경변수 설정

```bash
# Windows (PowerShell)
$env:GEMINI_API_KEY = "여기에_실제_API_키_입력"

# macOS / Linux
export GEMINI_API_KEY="여기에_실제_API_키_입력"
```

> **API 키 발급 방법:**  
> [Google AI Studio](https://aistudio.google.com/app/apikey) 접속 → `Create API Key` 클릭

### 5. 스크래퍼 실행

```bash
python scraper.py
```

실행 완료 후 `grants_data.json`과 `scraper.log`가 생성됩니다.

---

## 🔐 GitHub Secrets 설정 방법

GitHub Actions가 Gemini API를 사용하려면 API 키를 리포지토리 Secrets에 등록해야 합니다.

### 등록 절차

1. GitHub 리포지토리 페이지 접속
2. 상단 탭 **Settings** 클릭
3. 왼쪽 사이드바 **Secrets and variables** → **Actions** 클릭
4. **New repository secret** 버튼 클릭
5. 아래 정보 입력 후 **Add secret** 클릭:

| 항목 | 값 |
|------|-----|
| **Name** | `GEMINI_API_KEY` |
| **Secret** | 실제 Gemini API 키 문자열 |

### 확인

등록 후 Actions 탭에서 **Run workflow** 버튼으로 수동 테스트가 가능합니다.

---

## 📋 출력 데이터 스키마 (`grants_data.json`)

```json
[
  {
    "사업명": "2024년 소상공인 경영환경개선 지원사업",
    "지원대상": "관악구 소재 소상공인 (매출액 10억 미만)",
    "지원금액_또는_내용": "업체당 최대 200만원 (시설개선비 70% 지원)",
    "신청마감일": "2024-12-31",
    "원본공고링크": "https://www.gwanak.go.kr/...",
    "담당부서_연락처": "경제진흥과 02-879-6254"
  }
]
```

---

## ⏰ 자동화 스케줄

| 항목 | 내용 |
|------|------|
| 실행 시각 | 매일 **KST 08:00** (UTC 23:00) |
| 환경 | GitHub Actions `ubuntu-latest` |
| 트리거 | CRON 스케줄 + 수동 (`workflow_dispatch`) |
| 결과 | `grants_data.json` 변동 시 자동 커밋·푸시 |
| 로그 | GitHub Actions 아티팩트로 7일간 보관 |

---

## 🛡️ 안전 장치

- **암호화/손상 파일**: `try-except`로 감싸 에러 시 로그 기록 후 다음 파일로 진행
- **중복 방지**: 이미 처리된 URL은 스킵
- **API 레이트 리밋**: 파일 간 0.5초, 게시물 간 1초 딜레이 적용
- **텍스트 길이 제한**: 4,000자로 절단하여 Gemini 토큰 낭비 방지
- **강제 초기화**: `workflow_dispatch` 실행 시 `force_refresh=true` 옵션으로 전체 재수집 가능

---

## 🔧 트러블슈팅

| 증상 | 원인 | 해결책 |
|------|------|--------|
| `GEMINI_API_KEY` 환경변수 오류 | API 키 미설정 | `.env` 또는 GitHub Secrets 확인 |
| HWP 텍스트 추출 실패 | `hwp5txt` 미설치 | `pip install pyhwp` 재시도 또는 olefile 폴백 확인 |
| 게시물 0건 수집 | 게시판 HTML 구조 변경 | `scraper.py`의 CSS 셀렉터 업데이트 필요 |
| PDF에서 텍스트 없음 | 스캔본(이미지) PDF | OCR 도구(`pytesseract`) 추가 고려 |

---

## 📜 라이선스

MIT License - 자유롭게 사용, 수정, 배포 가능합니다.
