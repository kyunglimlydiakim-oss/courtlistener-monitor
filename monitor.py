#!/usr/bin/env python3
"""CourtListener 신규 소송 모니터.

watchlist.json의 기업이 당사자(party) 또는 사건명(caseName)에 포함된
미국 연방법원 신규 사건(RECAP docket)을 찾아 보고서를 쓰고 알림을 보낸다.

사용법
  python3 monitor.py                 # 지난 실행 이후 신규 사건 확인
  python3 monitor.py --since 2026-11-01 --no-notify   # 기간 지정 백필
환경변수
  COURTLISTENER_TOKEN   CourtListener API 토큰 (필수)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (선택) 텔레그램 알림
  SLACK_WEBHOOK_URL                      (선택) 슬랙 알림
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_TO   (선택) 이메일 알림
"""
import argparse
import csv
import datetime as dt
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

BASE = "https://www.courtlistener.com"
API = BASE + "/api/rest/v4/search/"
ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
REPORT_DIR = ROOT / "reports"
SEEN_FILE = STATE_DIR / "seen.json"
LAST_RUN_FILE = STATE_DIR / "last_run.txt"
LOG_CSV = STATE_DIR / "cases.csv"

LOOKBACK_DAYS = 2   # 법원 RSS·RECAP 반영 지연을 감안해 겹쳐서 조회한다
MAX_PAGES = 10
BATCH_SIZE = 30   # 한 번에 OR로 묶는 당사자명 수

# 기사 가치 판단에 쓰는 사건 유형 라벨 (Nature of Suit 일부)
NOS_LABEL = {
    "830": "특허", "835": "특허(ANDA)", "840": "상표", "820": "저작권",
    "410": "반독점", "850": "증권", "160": "주주소송", "190": "계약",
    "195": "계약(제조물)", "365": "제조물책임", "360": "상해", "385": "재산피해(제조물)",
    "370": "사기", "371": "대출진실법", "480": "소비자신용", "890": "기타 법률",
    "710": "노동(공정근로)", "442": "고용차별", "790": "노동 기타", "791": "ERISA",
}


class RateLimited(Exception):
    pass


REQUESTS_USED = 0


def http_get(url, token, retries=4):
    global REQUESTS_USED
    headers = {"User-Agent": "cl-monitor/1.0", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Token {token}"
    for i in range(retries):
        try:
            REQUESTS_USED += 1
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (401, 403):
                sys.exit(f"[인증 오류 {e.code}] 토큰을 확인하라: {body}")
            if e.code == 429:
                wait = int(e.headers.get("Retry-After") or 60)
                if wait > 120:   # 시간당 한도 소진: 기다리지 않고 다음 실행으로 넘긴다
                    raise RateLimited(f"API 한도 초과, {wait}초 후 재개 가능")
                time.sleep(wait)
                continue
            if e.code >= 500:
                time.sleep(10 * (i + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(10 * (i + 1))
    raise RuntimeError(f"요청 실패: {url}")


def chunk(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def search_batch(aliases, since, token, seen, first_run):
    """별칭 묶음을 한 번에 조회한다. 시간당 50회 한도(2026-11 실측) 때문에 묶음 조회가 필수다."""
    phrase = " OR ".join(f'"{a}"' for a in aliases)
    params = {
        "type": "r",                 # RECAP: 연방법원 사건(docket)
        "q": f"party:({phrase})",
        "filed_after": since.strftime("%m/%d/%Y"),
        "order_by": "dateFiled desc",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    out, pages = [], 0
    while url and pages < MAX_PAGES:
        data = http_get(url, token)
        results = data.get("results", [])
        out.extend(results)
        pages += 1
        # 평시에는 한 페이지 전체가 이미 본 사건이면 더 넘기지 않는다(요청 절약)
        if not first_run and results and all(str(r.get("docket_id")) in seen for r in results):
            break
        url = data.get("next")
    return out


# 기사 가치가 낮은 반복성 사건: 보고서 맨 아래로 묶고 텔레그램에선 건수만 알린다
ROUTINE_PATTERNS = {
    "Partnerships and Unincorporated Associations": "위조품 판매상 일괄 상표소송(Schedule A)",
}


def routine_tag(case_name):
    for pat, label in ROUTINE_PATTERNS.items():
        if pat.lower() in (case_name or "").lower():
            return label
    return ""


def nos_label(suit):
    if not suit:
        return ""
    code = suit.strip()[:3]
    return NOS_LABEL.get(code, suit)


def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def render_report(new_cases, since, now):
    lines = [
        f"# CourtListener 신규 소송 감지 ({now:%Y-%m-%d %H:%M} UTC)",
        "",
        f"- 조회 기간: {since:%Y-%m-%d} 이후 접수(dateFiled)",
        f"- 신규 사건: {len(new_cases)}건",
        "",
    ]
    routine = [c for c in new_cases if c.get("routine")]
    new_cases = [c for c in new_cases if not c.get("routine")]
    by_group = {}
    for c in new_cases:
        by_group.setdefault(c["group"], []).append(c)
    for group, cases in by_group.items():
        lines += [f"## {group} ({len(cases)}건)", ""]
        for c in sorted(cases, key=lambda x: x["dateFiled"] or "", reverse=True):
            lines += [
                f"### [{c['company']}] {c['caseName']}",
                f"- 접수일: {c['dateFiled']} · 법원: {c['court']} · 사건번호: {c['docketNumber']}",
                f"- 유형: {c['nos'] or '미기재'}" + (f" · 청구원인: {c['cause']}" if c['cause'] else ""),
                f"- 매칭 당사자명: {', '.join(c['hits']) or '(사건명/당사자 부분일치)'}",
                f"- 판사: {c['judge'] or '미기재'}",
                f"- 링크: {c['url']}",
                "",
            ]
    if routine:
        lines += [f"## 반복성 사건 ({len(routine)}건)", ""]
        for c in routine:
            lines.append(f"- [{c['company']}] {c['routine']} · {c['dateFiled']} · {c['court']} · {c['url']}")
        lines.append("")
    lines += [
        "---",
        "※ 자동 매칭 결과다. 동명 기업·소액 사건이 섞일 수 있으니 기사화 전 원문(소장)과 PACER로 확인한다.",
    ]
    return "\n".join(lines)


def short_text(new_cases):
    routine = [c for c in new_cases if c.get("routine")]
    new_cases = [c for c in new_cases if not c.get("routine")]
    rows = [f"⚖️ 신규 소송 {len(new_cases)}건 감지" + (f" (반복성 사건 {len(routine)}건 별도)" if routine else "")]
    for c in new_cases[:20]:
        rows.append(f"• [{c['company']}] {c['caseName'][:90]} | {c['court_id']} {c['dateFiled']} | {c['nos'] or '-'}\n  {c['url']}")
    if len(new_cases) > 20:
        rows.append(f"…외 {len(new_cases) - 20}건 (보고서 참조)")
    if routine:
        from collections import Counter
        for (comp, lab), n in Counter((c["company"], c["routine"]) for c in routine).items():
            rows.append(f"· {comp} {lab} {n}건")
    return "\n".join(rows)


def notify(new_cases, report_md):
    text = short_text(new_cases)
    tg_token, tg_chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
            data = urllib.parse.urlencode({"chat_id": tg_chat, "text": chunk,
                                           "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{tg_token}/sendMessage", data, timeout=30)
    slack = os.getenv("SLACK_WEBHOOK_URL")
    if slack:
        req = urllib.request.Request(slack, json.dumps({"text": text}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30)
    if os.getenv("SMTP_HOST") and os.getenv("MAIL_TO"):
        msg = MIMEText(report_md, "plain", "utf-8")
        msg["Subject"] = f"[소송 모니터] 신규 {len(new_cases)}건"
        msg["From"] = os.getenv("SMTP_USER", "monitor@localhost")
        msg["To"] = os.getenv("MAIL_TO")
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587"))) as s:
            s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD 이후 접수 사건 조회 (기본: 마지막 실행 - 3일)")
    ap.add_argument("--no-notify", action="store_true", help="알림 없이 보고서만 생성")
    ap.add_argument("--watchlist", default=str(ROOT / "watchlist.json"))
    ap.add_argument("--telegram-chat-id", action="store_true", help="봇에게 온 메시지에서 채팅 ID를 출력")
    ap.add_argument("--telegram-test", action="store_true", help="텔레그램 시험 메시지 발송")
    args = ap.parse_args()

    if args.telegram_chat_id:
        tok = os.environ["TELEGRAM_BOT_TOKEN"]
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=30) as r:
            upd = json.loads(r.read())
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("title") or u["message"]["chat"].get("first_name")
                 for u in upd.get("result", []) if "message" in u}
        print(chats or "메시지가 없다. 봇에게 먼저 아무 메시지나 보내라.")
        return
    if args.telegram_test:
        if not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
            sys.exit("[오류] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 비어 있다")
        notify([{"company": "테스트", "caseName": "Test v. Monitor", "court_id": "test",
                 "dateFiled": "-", "nos": "", "url": BASE}], "test")
        print("발송 완료")
        return

    token = os.getenv("COURTLISTENER_TOKEN", "")
    now = dt.datetime.now(dt.timezone.utc)
    STATE_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)

    seen = load_json(SEEN_FILE, {})
    first_run = not seen
    if args.since:
        since = dt.datetime.strptime(args.since, "%Y-%m-%d")
    elif LAST_RUN_FILE.exists():
        last = dt.datetime.fromisoformat(LAST_RUN_FILE.read_text().strip())
        since = last - dt.timedelta(days=LOOKBACK_DAYS)
    else:
        since = now - dt.timedelta(days=7)

    watch = load_json(Path(args.watchlist), {})["groups"]
    alias_map = []   # (별칭, 그룹, 기업)
    for group, companies in watch.items():
        for company, aliases in companies.items():
            alias_map += [(a, group, company) for a in aliases]
    all_aliases = list(dict.fromkeys(a for a, _, _ in alias_map))

    new_cases, aborted = [], None
    for batch in chunk(all_aliases, BATCH_SIZE):
        try:
            results = search_batch(batch, since, token, seen, first_run)
        except RateLimited as e:
            aborted = str(e)
            break
        except RuntimeError as e:
            print(f"[경고] 묶음 조회 실패: {e}", file=sys.stderr)
            continue
        for r in results:
            key = str(r.get("docket_id"))
            if key in seen:
                continue
            hay = " ".join([r.get("caseName") or ""] + list(r.get("party") or [])).lower()
            hits = [(a, g, c) for a, g, c in alias_map if a.lower() in hay]
            group = hits[0][1] if hits else "기타(검색 매칭)"
            company = " / ".join(dict.fromkeys(c for _, _, c in hits)) or "미분류"
            seen[key] = now.isoformat()
            new_cases.append({
                "group": group,
                "company": company,
                "caseName": r.get("caseName") or "",
                "dateFiled": r.get("dateFiled") or "",
                "court": r.get("court") or "",
                "court_id": r.get("court_id") or "",
                "docketNumber": r.get("docketNumber") or "",
                "nos": nos_label(r.get("suitNature")),
                "cause": r.get("cause") or "",
                "judge": r.get("assignedTo") or "",
                "hits": list(dict.fromkeys(a for a, _, _ in hits)),
                "url": BASE + (r.get("docket_absolute_url") or ""),
                "routine": routine_tag(r.get("caseName")),
            })
        time.sleep(1)

    print(f"API 요청 {REQUESTS_USED}회 사용")
    if aborted:
        print(f"[중단] {aborted}. 이번 결과까지만 저장한다.", file=sys.stderr)
    print(f"조회 기준일 {since:%Y-%m-%d}, 신규 {len(new_cases)}건")
    if not new_cases:
        (REPORT_DIR / "latest_new.json").write_text(
            json.dumps({"generated": now.isoformat(), "report": None, "cases": []}), encoding="utf-8")
    if new_cases:
        report = render_report(new_cases, since, now)
        rp = REPORT_DIR / f"{now:%Y%m%d_%H%M}.md"
        rp.write_text(report, encoding="utf-8")
        # 한국 보도 여부 대조 단계(예약 실행의 Claude)가 읽는 파일
        (REPORT_DIR / "latest_new.json").write_text(
            json.dumps({"generated": now.isoformat(), "report": rp.name, "cases": new_cases},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"보고서: {rp}")
        new_file = not LOG_CSV.exists()
        with LOG_CSV.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["감지시각", "그룹", "기업", "사건명", "접수일", "법원", "사건번호", "유형", "링크"])
            for c in new_cases:
                w.writerow([now.isoformat(), c["group"], c["company"], c["caseName"], c["dateFiled"],
                            c["court"], c["docketNumber"], c["nos"], c["url"]])
        if not args.no_notify and not first_run:
            notify(new_cases, report)
        elif first_run:
            print("첫 실행이라 알림은 보내지 않았다(기존 사건 대량 발송 방지).")

    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")
    if not aborted:
        LAST_RUN_FILE.write_text(now.isoformat())


if __name__ == "__main__":
    main()
