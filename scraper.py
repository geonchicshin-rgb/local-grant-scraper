"""
지자체 공고 수집 및 AI 정제 파이프라인
대상: 서울시 관악구 · 서초구 · 강남구 고시/공고 게시판
출력: grants_data.json (소상공인/자영업자/중소기업 대상 지원금 정보)
"""

import os
import io
import re
import json
import logging
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
import html
import urllib3
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
import pdfplumber
import google.generativeai as genai

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
# 멀티 타겟 설정 (구청별 게시판 URL + CSS 셀렉터)
# ──────────────────────────────────────────────
TARGETS = [
    {
        "name": "관악구",
        "base_url": "https://www.gwanak.go.kr",
        "board_url": "https://www.gwanak.go.kr/site/gwanak/ex/bbsNew/List.do?typeCode=1",
        "selectors": [
            "table tbody tr td.subject a",
            ".board-list table tbody tr td.subject a",
        ],
        "attach_keywords": [".hwp", ".pdf", "fileDown", "download", "atch"],
        "view_url_template": "https://www.gwanak.go.kr/site/gwanak/ex/bbsNew/View.do?not_ancmt_mgt_no={id}&typeCode=1"
    },
    {
        "name": "서초구",
        "base_url": "https://www.seocho.go.kr",
        "board_url": "https://www.seocho.go.kr/site/seocho/ex/bbs/List.do?cbIdx=57",
        "selectors": [
            "table.bbs-list td.subject a",
            "table tbody tr td.subject a",
            "table tbody tr td a[href*='View']",
        ],
        "attach_keywords": [".hwp", ".pdf", "fileDown", "download", "atch"],
    },
    {
        "name": "강남구",
        "base_url": "https://www.gangnam.go.kr",
        "board_url": "https://www.gangnam.go.kr/notice/list.do?mid=ID05_040201",
        "selectors": [
            "table.table-style tbody tr td a[href*='view']",
            "table.table-style tbody tr td a",
            "table tbody tr td a[href*='notice']",
        ],
        "attach_keywords": [".hwp", ".pdf", "fileDown", "download", "atch", "file"],
    },
]

OUTPUT_FILE = Path("grants_data.json")
REQUEST_TIMEOUT = 30
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
}

GEMINI_FILTER_PROMPT = """
당신은 지자체 공고문 필터링 전문가입니다.
주어진 공고 제목과 짧은 본문 요약을 보고, 이 게시물이 '소상공인, 자영업자, 중소기업, 창업'을 대상으로 하는 '지원금, 보조금, 혜택, 융자, 교육지원, 컨설팅 등'의 실질적 지원사업 공고인지 판단하세요.
만약 대상 공고가 맞다면 "TRUE", 관련 없는 공고이면 "FALSE" 라고만 응답하세요.
"""

GEMINI_EXTRACT_PROMPT = """
당신은 지자체 공고문 분석 전문가입니다.
주어진 텍스트에서 지원사업의 핵심 정보를 추출하세요. 만일 지원금이나 혜택 정보가 없다면 반드시 null 을 반환하세요.
JSON 스키마 (코드블록 없이 순수 JSON만 반환):
{
  "사업명": "사업/공고 전체 명칭",
  "지원대상": "지원 대상 자격 조건 상세",
  "지원금액_또는_내용": "지원 금액 또는 지원 내용 상세",
  "신청마감일": "YYYY-MM-DD 형식 (불명확하면 '미정')",
  "원본공고링크": "원본 공고 URL",
  "담당부서_연락처": "담당 부서명 및 전화번호",
  "출처": "수집 출처 구청명"
}
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

                for entry in ole.listdir():
                    entry_name = "/".join(entry)
                    if "BodyText" in entry_name or "PrvText" in entry_name:
                        try:
                            stream = ole.openstream(entry)
                            raw = stream.read()
                            try:
                                decoded = raw.decode("utf-16-le", errors="ignore")
                                cleaned = "".join(
                                    c for c in decoded if c.isprintable() or c in "\n\t "
                                )
                                if cleaned.strip():
                                    text_parts.append(cleaned.strip())
                            except Exception:
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
# HWPX 텍스트 추출
# ──────────────────────────────────────────────

def extract_text_from_hwpx(file_bytes: bytes) -> Optional[str]:
    """HWPX 파일(ZIP-XML)에서 텍스트를 추출합니다."""
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            text_parts = []
            for item in zf.namelist():
                if item.startswith("Contents/section") and item.endswith(".xml"):
                    xml_content = zf.read(item)
                    tree = ET.fromstring(xml_content)
                    for elem in tree.iter():
                        if elem.tag.endswith('}t') and elem.text:
                            text_parts.append(elem.text)
            if text_parts:
                log.info("HWPX 텍스트 추출 성공")
                return "\n".join(text_parts)
            else:
                log.warning("HWPX에서 추출된 텍스트가 없습니다.")
                return None
    except Exception as e:
        log.error(f"HWPX 텍스트 추출 중 오류: {e}")
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
        resp = session.get(file_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, stream=True, verify=False)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "").lower()
        file_bytes = resp.content
        cd = resp.headers.get("Content-Disposition", "").lower()
        url_lower = file_url.lower()

        if url_lower.endswith(".pdf") or "pdf" in content_type or ".pdf" in cd:
            return extract_text_from_pdf(file_bytes)
        elif url_lower.endswith(".hwpx") or "hwpx" in content_type or ".hwpx" in cd:
            return extract_text_from_hwpx(file_bytes)
        elif (url_lower.endswith(".hwp") or "hwp" in content_type
              or "haansofthwp" in content_type or ".hwp" in cd):
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
# Gemini AI 모델 설정 및 상구조화
# ──────────────────────────────────────────────

_AVAILABLE_MODELS_CACHE: list[str] = []

def get_best_model(model_type: str = "flash") -> str:
    """사용자 계정에서 사용 가능한 가장 좋은 모델명을 동적으로 반환합니다."""
    global _AVAILABLE_MODELS_CACHE
    
    if not _AVAILABLE_MODELS_CACHE:
        try:
            log.info("사용 가능한 Gemini 모델 목록 조회 중...")
            model_list = [m.name.replace("models/", "") for m in genai.list_models()]
            _AVAILABLE_MODELS_CACHE = model_list
            log.debug(f"감지된 모델: {', '.join(model_list)}")
        except Exception as e:
            log.warning(f"모델 목록 조회 실패 (기본값 사용): {e}")
            return "gemini-1.5-pro" if model_type == "pro" else "gemini-1.5-flash"

    # 우선 순서대로 매칭
    if model_type == "pro":
        priorities = ["gemini-3.1-pro", "gemini-1.5-pro", "gemini-pro"]
    else:
        priorities = ["gemini-3.1-flash", "gemini-1.5-flash", "gemini-1.5-flash-latest"]

    for p in priorities:
        if any(p in m for m in _AVAILABLE_MODELS_CACHE):
            target = next(m for m in _AVAILABLE_MODELS_CACHE if p in m)
            return target

    return priorities[1] # 못 찾으면 기본값(1.5) 반환

def filter_with_flash(title: str, body: str, source_name: str) -> bool:
    if not GEMINI_API_KEY:
        return True
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        truncated = body[:1000] if len(body) > 1000 else body
        prompt = f"""출처: {source_name}
제목: {title}
요약: {truncated}"""
        
        model_name = get_best_model("flash")
        model = genai.GenerativeModel(model_name=model_name, system_instruction=GEMINI_FILTER_PROMPT)
        resp = model.generate_content(prompt)
        res = resp.text.strip().upper()
        
        if "TRUE" in res:
            return True
        log.info(f"[{source_name}][필터링됨] AI 판단: 대상 아님 ({title})")
        return False
    except Exception as e:
        if "429" in str(e):
            log.error(f"Gemini API 한도 초과 (429): 결제 수단 또는 한도를 확인하세요.")
        else:
            log.warning(f"AI 필터 실패(통과처리): {e}")
        return True

def extract_with_pro(text: str, post_url: str, source_name: str) -> Optional[dict]:
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY 없음")
        return None
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        truncated = text[:10000] if len(text) > 10000 else text
        prompt = f"""출처: {source_name}
원본공고링크: {post_url}

공고 본문:
{truncated}"""
        
        model_name = get_best_model("pro")
        model = genai.GenerativeModel(model_name=model_name, system_instruction=GEMINI_EXTRACT_PROMPT)
        resp = model.generate_content(prompt)
        raw = resp.text.strip()
        
        if "null" in raw.lower() and len(raw) < 10:
            return None
            
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            
        import json
        parsed = json.loads(raw)
        parsed["원본공고링크"] = post_url
        parsed["출처"] = source_name
        log.info(f"[{source_name}] AI 추출 완료: {parsed.get('사업명', '미상')}")
        return parsed
    except Exception as e:
        if "429" in str(e):
            log.error(f"Gemini API 한도 초과 (429): AI Studio의 Spending Cap 설정을 확인해 주세요.")
        else:
            log.error(f"AI 추출 에러: {e}")
        return None


# ──────────────────────────────────────────────
# 구청별 게시판 파싱
# ──────────────────────────────────────────────

def fetch_board_posts(target: dict, session: requests.Session) -> list[dict]:
    """구청 고시/공고 게시판 1페이지 게시물 목록을 수집합니다."""
    name = target["name"]
    board_url = target["board_url"]
    base_url = target["base_url"]
    selectors = target["selectors"]
    posts = []

    try:
        log.info(f"[{name}] 게시판 접근 중: {board_url}")
        resp = session.get(board_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        links = []
        # 우선순위 셀렉터 순서대로 시도
        for selector in selectors:
            links = soup.select(selector)
            if links:
                log.info(f"[{name}] 셀렉터 '{selector}' 로 {len(links)}개 링크 감지")
                break

        # 폴백: 모든 <a> 중 게시물 링크처럼 보이는 것 필터링
        if not links:
            log.warning(f"[{name}] 정의된 셀렉터로 링크 미감지 → 폴백 파싱")
            all_links = soup.find_all("a", href=True)
            links = [
                a for a in all_links
                if any(kw in a["href"] for kw in ["View", "view", "detail", "read"])
                and a.get_text(strip=True)
            ]

        seen_urls = set()
        for a in links:
            try:
                title = a.get_text(strip=True)
                href = a.get("href", "")
                onclick = a.get("onclick", "")
                post_url = None

                # 1. onclick="fncView('...')" 형태의 ID 추출 (관악구 등)
                if onclick and "fncView" in onclick:
                    match = re.search(r"fncView\('(\d+)'\)", onclick)
                    if match and target.get("view_url_template"):
                        post_id = match.group(1)
                        post_url = target["view_url_template"].format(id=post_id)

                # 2. 일반 href 속성 처리
                if not post_url and href and not href.startswith("#"):
                    if href.startswith("/"):
                        post_url = base_url + href
                    elif href.startswith("http"):
                        post_url = href
                    else:
                        post_url = base_url + "/" + href

                if not title or not post_url or post_url in seen_urls:
                    continue

                seen_urls.add(post_url)
                posts.append({"title": title, "url": post_url})
                log.debug(f"[{name}] 게시물: {title[:40]}")

            except Exception as e:
                log.warning(f"[{name}] 링크 파싱 오류 (무시): {e}")
                continue

        log.info(f"[{name}] 총 {len(posts)}개 게시물 수집 완료")

    except requests.exceptions.RequestException as e:
        log.error(f"[{name}] 게시판 접근 실패: {e}")
    except Exception as e:
        log.error(f"[{name}] 게시판 파싱 오류: {e}")

    return posts


def fetch_post_attachments(
    post_url: str,
    base_url: str,
    attach_keywords: list[str],
    session: requests.Session,
) -> list[str]:
    """게시물 상세 페이지에서 첨부파일(HWP, PDF) URL을 수집합니다."""
    attachment_urls = []
    try:
        resp = session.get(post_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        # <a> 태그에서 첨부파일 링크 탐색
        for a in soup.find_all("a", href=True):
            href = a["href"]
            href_lower = href.lower()
            if any(kw.lower() in href_lower for kw in attach_keywords):
                if href.startswith("/"):
                    file_url = base_url + href
                elif href.startswith("http"):
                    file_url = href
                else:
                    file_url = base_url + "/" + href
                    
                # 뷰어/미리보기 링크 제외
                if any(exclude_kw in file_url.lower() for exclude_kw in ["preimagefromdoc", "viewer", "preview"]):
                    continue
                    
                attachment_urls.append(file_url)

        # onclick 속성에서 다운로드 URL 추출
        for tag in soup.find_all(onclick=True):
            onclick = tag["onclick"]
            if "download" in onclick.lower() or "file" in onclick.lower():
                found = re.findall(
                    r"['\"]([^'\"]*(?:hwp|pdf|fileDown|download)[^'\"]*)['\"]",
                    onclick,
                    re.IGNORECASE,
                )
                for u in found:
                    if u.startswith("/"):
                        file_url = base_url + u
                    elif u.startswith("http"):
                        file_url = u
                    else:
                        continue
                        
                    if any(exclude_kw in file_url.lower() for exclude_kw in ["preimagefromdoc", "viewer", "preview"]):
                        continue
                        
                    attachment_urls.append(file_url)

        log.info(f"  첨부파일 {len(set(attachment_urls))}개 발견")

    except requests.exceptions.RequestException as e:
        log.error(f"게시물 상세 접근 실패 ({post_url}): {e}")
    except Exception as e:
        log.error(f"첨부파일 파싱 오류 ({post_url}): {e}")

    return list(set(attachment_urls))


def send_telegram_message(new_grants: list[dict]) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("텔레그램 토큰 또는 Chat ID가 설정되지 않아 알림을 스킵합니다.")
        return
        
    if not new_grants:
        return
        
    text = f"🔔 오늘의 신규 지원사업: {len(new_grants)}건\n\n"
    for idx, item in enumerate(new_grants, 1):
        name = html.escape(item.get('사업명', '미상'))
        src = html.escape(item.get('출처', '미상'))
        deadline = html.escape(item.get('신청마감일', '미정'))
        text += f"{idx}. [{src}] <b>{name}</b>\n⏳ 마감: {deadline}\n\n"
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            log.error(f"텔레그램 알림 발송 실패 (Status: {resp.status_code})")
            log.error(f"응답 본문: {resp.text}")
        else:
            log.info("텔레그램 알림 발송 성공!")
    except Exception as e:
        log.error(f"텔레그램 알림 발송 중 예외 발생: {e}")

# # ──────────────────────────────────────────────
# 메인 파이프라인
# ──────────────────────────────────────────────

def run_pipeline() -> None:
    """전체 수집→파싱→AI분석→저장 파이프라인을 실행합니다."""
    log.info("=" * 60)
    log.info(f"파이프라인 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"수집 대상: {', '.join(t['name'] for t in TARGETS)}")
    log.info("=" * 60)

    session = requests.Session()
    session.headers.update(HEADERS)
    session.verify = False  # SSL 인증서 검증 무시 (강남구 등)

    # 기존 데이터 로드
    existing_data: list[dict] = []
    existing_urls: set[str] = set()
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE, encoding="utf-8") as f:
                existing_data = json.load(f)
            existing_urls = {item.get("원본공고링크", "") for item in existing_data}
            log.info(f"기존 데이터 {len(existing_data)}건 로드")
        except Exception as e:
            log.error(f"기존 데이터 로드 실패 (초기화): {e}")

    new_grants: list[dict] = []

    # ── 구청별 순회 ──
    for target in TARGETS:
        name = target["name"]
        base_url = target["base_url"]
        attach_keywords = target["attach_keywords"]

        log.info(f"\n{'─'*40}")
        log.info(f"[{name}] 수집 시작")
        log.info(f"{'─'*40}")

        posts = fetch_board_posts(target, session)
        if not posts:
            log.warning(f"[{name}] 수집된 게시물 없음. 다음 구청으로 이동.")
            continue

        for post in posts:
            post_url = post["url"]
            post_title = post["title"]

            if post_url in existing_urls:
                log.info(f"[{name}][SKIP] {post_title[:35]}")
                continue

            log.info(f"[{name}][처리중] {post_title[:35]}")

            # 본문 추출 (Flash 필터링 용도)
            body_text = ""
            try:
                resp = session.get(post_url, headers=HEADERS, timeout=REQUEST_TIMEOUT, verify=False)
                soup = BeautifulSoup(resp.text, "html.parser")
                area = (
                    soup.find(class_="view_cont")
                    or soup.find(class_="bbs_view")
                    or soup.find(class_="view-content")
                    or soup.find("article")
                    or soup.find(id="content")
                )
                if area:
                    body_text = area.get_text(separator="\n", strip=True)
            except Exception as e:
                log.error(f"본문 추출 에러: {e}")
                
            # Flash 모델로 1차 필터링
            is_target = filter_with_flash(post_title, body_text, name)
            
            if not is_target:
                continue
                
            # 타겟 공고로 확인된 경우, 첨부파일 수집 및 Pro 모델 가동
            attachments = fetch_post_attachments(
                post_url, base_url, attach_keywords, session
            )

            extracted_texts: list[str] = []
            if attachments:
                for file_url in attachments:
                    text = download_and_extract(file_url, session)
                    if text:
                        extracted_texts.append(text)
                    time.sleep(0.5)
            
            if body_text.strip() and not extracted_texts:
                extracted_texts.append(body_text)

            # Gemini Pro 구조화
            for text in extracted_texts:
                if len(text.strip()) < 50:
                    continue
                result = extract_with_pro(text, post_url, name)
                if result:
                    new_grants.append(result)
                    break # 하나라도 성공하면 완료
                time.sleep(1)

            time.sleep(1)  # 게시물 간 딜레이

        log.info(f"[{name}] 처리 완료")
        time.sleep(2)  # 구청 간 딜레이

    # 저장
    final_data = existing_data + new_grants
    final_data.sort(key=lambda x: x.get("신청마감일", "9999"))

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_data, f, ensure_ascii=False, indent=2)
        
    if new_grants:
        send_telegram_message(new_grants)

    log.info("=" * 60)
    log.info(f"파이프라인 완료")
    log.info(f"신규 수집: {len(new_grants)}건 | 누적: {len(final_data)}건")
    log.info(f"결과 저장: {OUTPUT_FILE.resolve()}")
    log.info("=" * 60)


if __name__ == "__main__":
    run_pipeline()
