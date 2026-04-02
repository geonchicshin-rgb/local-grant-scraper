"""
지자체 공고 수집 및 AI 정제 파이프라인
대상: 서울시 관악구청 고시/공고 게시판
출력: grants_data.json (소상공인/자영업자/중소기업 대상 지원금 정보)
"""

import os
import io
import json
import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
import pdfplumber
import google.generativeai as genai

# ──────────────────────────────────────────────
# 로깅 설정
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 상수 설정
# ──────────────────────────────────────────────
BASE_URL = "https://www.gwanak.go.kr"
BOARD_URL = f"{BASE_URL}/site/gwanak/ex/bbs/List.do?cbIdx=1237"
OUTPUT_FILE = Path("grants_data.json")
REQUEST_TIMEOUT = 30
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}

GEMINI_SYSTEM_PROMPT = """
당신은 지자체 공고문 분석 전문가입니다.
주어진 텍스트가 '소상공인, 자영업자, 중소기업'을 대상으로 하는 
'지원금, 보조금, 환급금, 융자, 지원사업' 공고인지 판단하세요.

판단 기준:
- 대상이 아닌 경우: 정확히 null 단어만 반환 (다른 텍스트 없이)
- 대상인 경우: 아래 JSON 스키마로 정확히 구조화하여 반환

JSON 스키마 (코드블록 없이 순수 JSON만 반환):
{
  "사업명": "사업/공고 전체 명칭",
  "지원대상": "지원 대상 자격 조건 상세",
  "지원금액_또는_내용": "지원 금액 또는 지원 내용 상세",
  "신청마감일": "YYYY-MM-DD 형식 (불명확하면 '미정')",
  "원본공고링크": "원본 공고 URL",
  "담당부서_연락처": "담당 부서명 및 전화번호"
}

반드시 유효한 JSON 또는 null 만 반환하세요.
"""

# ──────────────────────────────────────────────
# HWP 텍스트 추출
# ──────────────────────────────────────────────

def extract_text_from_hwp(file_bytes: bytes) -> Optional[str]:
    """HWP 파일에서 텍스트를 추출합니다."""
    try:
        import olefile
        with tempfile.NamedTemporaryFile(suffix=".hwp", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name

        try:
            # pyhwp (hwp5txt) 방식 우선 시도
            try:
                import subprocess
                result = subprocess.run(
                    ["hwp5txt", tmp_path],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                )
                if result.returncode == 0 and result.stdout.strip():
                    log.info("hwp5txt로 HWP 텍스트 추출 성공")
                    return result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                log.debug(f"hwp5txt 불가, olefile 방식 시도: {e}")

            # olefile 방식 폴백
            if olefile.isOleFile(tmp_path):
                ole = olefile.OleFileIO(tmp_path)
                text_parts = []

                # HWP BodyText 스트림에서 텍스트 추출 시도
                for entry in ole.listdir():
                    entry_name = "/".join(entry)
                    if "BodyText" in entry_name or "PrvText" in entry_name:
                        try:
                            stream = ole.openstream(entry)
                            raw = stream.read()
                            # UTF-16 LE 디코딩 시도
                            try:
                                decoded = raw.decode("utf-16-le", errors="ignore")
                                cleaned = "".join(
                                    c for c in decoded if c.isprintable() or c in "\n\t "
                                )
                                if cleaned.strip():
                                    text_parts.append(cleaned.strip())
                            except Exception:
                                # EUC-KR 시도
                                try:
                                    decoded = raw.decode("euc-kr", errors="ignore")
                                    if decoded.strip():
                                        text_parts.append(decoded.strip())
                                except Exception:
                                    pass
                        except Exception as stream_err:
                            log.debug(f"스트림 읽기 실패: {stream_err}")

                ole.close()
                if text_parts:
                    log.info("olefile로 HWP 텍스트 추출 성공")
                    return "\n".join(text_parts)

            log.warning("HWP에서 텍스트를 추출하지 못했습니다.")
            return None

        finally:
            Path(tmp_path).unlink(missing_ok=True)

    except Exception as e:
        log.error(f"HWP 텍스트 추출 중 오류: {e}")
        return None


# ──────────────────────────────────────────────
# PDF 텍스트 추출
# ──────────────────────────────────────────────

def extract_text_from_pdf(file_bytes: bytes) -> Optional[str]:
    """PDF 파일에서 텍스트를 추출합니다."""
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            pages_text = []
            for i, page in enumerate(pdf.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        pages_text.append(page_text)
                except Exception as page_err:
                    log.warning(f"PDF 페이지 {i+1} 추출 실패: {page_err}")
                    continue

            if pages_text:
                log.info(f"PDF에서 {len(pages_text)}페이지 텍스트 추출 성공")
                return "\n".join(pages_text)
            else:
                log.warning("PDF에서 추출된 텍스트가 없습니다 (스캔본일 수 있음).")
                return None
    except Exception as e:
        log.error(f"PDF 텍스트 추출 중 오류: {e}")
        return None


# ──────────────────────────────────────────────
# 파일 다운로드 및 텍스트 추출 라우터
# ──────────────────────────────────────────────

def download_and_extract(file_url: str, session: requests.Session) -> Optional[str]:
    """파일을 다운로드하고 형식에 맞게 텍스트를 추출합니다."""
    try:
        log.info(f"파일 다운로드 중: {file_url}")
        resp = session.get(file_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").lower()
        file_bytes = resp.content

        url_lower = file_url.lower()
        if url_lower.endswith(".pdf") or "pdf" in content_type:
            return extract_text_from_pdf(file_bytes)
        elif url_lower.endswith(".hwp") or "hwp" in content_type or "haansofthwp" in content_type:
            return extract_text_from_hwp(file_bytes)
        else:
            # Content-Disposition에서 파일명 확인
            cd = resp.headers.get("Content-Disposition", "")
            if ".pdf" in cd.lower():
                return extract_text_from_pdf(file_bytes)
            elif ".hwp" in cd.lower():
                return extract_text_from_hwp(file_bytes)
            else:
                log.warning(f"지원하지 않는 파일 형식: {content_type} / URL: {file_url}")
                return None

    except requests.exceptions.RequestException as e:
        log.error(f"파일 다운로드 실패 ({file_url}): {e}")
        return None
    except Exception as e:
        log.error(f"파일 처리 중 예외 발생 ({file_url}): {e}")
        return None


# ──────────────────────────────────────────────
# Gemini AI 필터링 및 구조화
# ──────────────────────────────────────────────

def analyze_with_gemini(text: str, post_url: str) -> Optional[dict]:
    """Gemini API로 텍스트를 분석하여 지원금 공고 여부를 판단하고 구조화합니다."""
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.")
        return None

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=GEMINI_SYSTEM_PROMPT,
        )

        # 텍스트가 너무 길면 앞 4000자만 사용 (토큰 절약)
        truncated_text = text[:4000] if len(text) > 4000 else text
        prompt = f"원본공고링크: {post_url}\n\n공고 텍스트:\n{truncated_text}"

        response = model.generate_content(prompt)
        raw = response.text.strip()

        if raw.lower() == "null" or raw == "":
            log.info("Gemini: 지원금 공고 아님 (null 반환)")
            return None

        # 코드블록 제거
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        parsed = json.loads(raw)
        parsed["원본공고링크"] = post_url  # 링크 덮어쓰기 보정
        log.info(f"Gemini: 지원금 공고 감지 → 사업명: {parsed.get('사업명', '미상')}")
        return parsed

    except json.JSONDecodeError as e:
        log.error(f"Gemini 응답 JSON 파싱 실패: {e}\n응답: {raw[:200]}")
        return None
    except Exception as e:
        log.error(f"Gemini API 호출 중 오류: {e}")
        return None


# ──────────────────────────────────────────────
# 게시판 파싱
# ──────────────────────────────────────────────

def fetch_board_posts(session: requests.Session) -> list[dict]:
    """관악구청 고시/공고 게시판 1페이지 게시물 목록을 수집합니다."""
    posts = []
    try:
        log.info(f"게시판 접근 중: {BOARD_URL}")
        resp = session.get(BOARD_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"

        soup = BeautifulSoup(resp.text, "html.parser")

        # 게시물 행 파싱 (일반적인 지자체 게시판 구조)
        rows = soup.select("table.board_list tbody tr, ul.board_list li, .bbs_list tr")
        if not rows:
            # 더 넓은 셀렉터로 재시도
            rows = soup.select("tr")
            rows = [r for r in rows if r.find("a")]

        for row in rows:
            try:
                link_tag = row.find("a", href=True)
                if not link_tag:
                    continue

                title = link_tag.get_text(strip=True)
                href = link_tag["href"]

                # 상대 URL 보정
                if href.startswith("/"):
                    post_url = BASE_URL + href
                elif href.startswith("http"):
                    post_url = href
                else:
                    post_url = BASE_URL + "/" + href

                if title and post_url:
                    posts.append({"title": title, "url": post_url})
                    log.debug(f"게시물 수집: {title[:40]}")

            except Exception as row_err:
                log.warning(f"행 파싱 중 오류 (무시): {row_err}")
                continue

        log.info(f"총 {len(posts)}개 게시물 수집 완료")

    except requests.exceptions.RequestException as e:
        log.error(f"게시판 접근 실패: {e}")
    except Exception as e:
        log.error(f"게시판 파싱 중 오류: {e}")

    return posts


def fetch_post_attachments(post_url: str, session: requests.Session) -> list[str]:
    """게시물 상세 페이지에서 첨부파일(HWP, PDF) URL을 수집합니다."""
    attachment_urls = []
    try:
        resp = session.get(post_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"

        soup = BeautifulSoup(resp.text, "html.parser")

        # 첨부파일 링크 탐색
        for a in soup.find_all("a", href=True):
            href = a["href"]
            href_lower = href.lower()
            if any(ext in href_lower for ext in [".hwp", ".pdf", "fileDown", "download", "atch"]):
                if href.startswith("/"):
                    file_url = BASE_URL + href
                elif href.startswith("http"):
                    file_url = href
                else:
                    file_url = BASE_URL + "/" + href
                attachment_urls.append(file_url)

        # onclick 속성에서 다운로드 URL 추출
        for tag in soup.find_all(onclick=True):
            onclick = tag["onclick"]
            if "download" in onclick.lower() or "file" in onclick.lower():
                import re
                urls = re.findall(r"['\"]([^'\"]*(?:hwp|pdf|fileDown)[^'\"]*)['\"]", onclick, re.IGNORECASE)
                for u in urls:
                    if u.startswith("/"):
                        attachment_urls.append(BASE_URL + u)
                    elif u.startswith("http"):
                        attachment_urls.append(u)

        log.info(f"  첨부파일 {len(attachment_urls)}개 발견: {post_url[:60]}")

    except requests.exceptions.RequestException as e:
        log.error(f"게시물 상세 페이지 접근 실패 ({post_url}): {e}")
    except Exception as e:
        log.error(f"첨부파일 파싱 중 오류 ({post_url}): {e}")

    return list(set(attachment_urls))  # 중복 제거


# ──────────────────────────────────────────────
# 메인 파이프라인
# ──────────────────────────────────────────────

def run_pipeline() -> None:
    """전체 수집→파싱→AI분석→저장 파이프라인을 실행합니다."""
    log.info("=" * 60)
    log.info(f"파이프라인 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 60)

    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. 게시물 목록 수집
    posts = fetch_board_posts(session)
    if not posts:
        log.warning("수집된 게시물이 없습니다. 파이프라인 종료.")
        return

    # 2. 기존 데이터 로드
    existing_data = []
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                existing_data = json.load(f)
            existing_urls = {item.get("원본공고링크") for item in existing_data}
            log.info(f"기존 데이터 {len(existing_data)}건 로드 완료")
        except Exception as e:
            log.error(f"기존 데이터 로드 실패 (초기화): {e}")
            existing_urls = set()
    else:
        existing_urls = set()

    # 3. 각 게시물 처리
    new_grants = []
    for post in posts:
        post_url = post["url"]
        post_title = post["title"]

        # 이미 처리된 URL 스킵
        if post_url in existing_urls:
            log.info(f"[SKIP] 이미 처리된 게시물: {post_title[:40]}")
            continue

        log.info(f"[처리중] {post_title[:40]}")

        # 4. 첨부파일 URL 수집
        attachments = fetch_post_attachments(post_url, session)

        extracted_texts = []

        # 5. 첨부파일 다운로드 및 텍스트 추출
        if attachments:
            for file_url in attachments:
                text = download_and_extract(file_url, session)
                if text:
                    extracted_texts.append(text)
                time.sleep(0.5)  # 서버 부하 방지
        else:
            # 첨부파일 없을 경우 게시물 본문 텍스트 활용
            try:
                resp = session.get(post_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
                soup = BeautifulSoup(resp.text, "html.parser")
                # 본문 영역 추출 시도
                content_area = (
                    soup.find(class_="view_cont")
                    or soup.find(class_="bbs_view")
                    or soup.find("article")
                    or soup.find(id="content")
                )
                if content_area:
                    body_text = content_area.get_text(separator="\n", strip=True)
                    if body_text.strip():
                        extracted_texts.append(body_text)
            except Exception as e:
                log.error(f"본문 추출 실패: {e}")

        # 6. Gemini AI 분석
        for text in extracted_texts:
            if len(text.strip()) < 50:
                log.debug("텍스트가 너무 짧아 분석 생략")
                continue

            result = analyze_with_gemini(text, post_url)
            if result:
                new_grants.append(result)
                break  # 동일 게시물에서 첫 번째 유효 결과만 사용

            time.sleep(1)  # API 레이트 리밋 방지

        time.sleep(1)  # 게시물 간 딜레이

    # 7. 데이터 저장
    final_data = existing_data + new_grants
    final_data.sort(key=lambda x: x.get("신청마감일", "9999"), reverse=False)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)

    log.info("=" * 60)
    log.info(f"파이프라인 완료: 신규 {len(new_grants)}건 추가, 누적 {len(final_data)}건")
    log.info(f"결과 저장: {OUTPUT_FILE.resolve()}")
    log.info("=" * 60)


if __name__ == "__main__":
    run_pipeline()
