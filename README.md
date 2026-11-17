# CourtListener 소송 모니터

한국 기업·글로벌 반도체 기업이 당사자로 오른 **미국 연방법원 신규 사건**을 CourtListener에서 하루 두 번(KST 07:37, 14:37) Claude 예약 작업이 확인하고 Gmail로 알린다. GitHub Actions는 수동 실행용 예비 수단이다.

## 구성
- `watchlist.json` 감시 기업과 영문 법인명. 자유롭게 추가·삭제한다.
- `monitor.py` 검색 → 중복 제거 → 보고서(`reports/*.md`) → 누적 로그(`state/cases.csv`) → 알림.
- `.github/workflows/monitor.yml` GitHub Actions로 30분 주기 자동 실행.

## 설치 (GitHub Actions, 무료)
1. 이 폴더를 **비공개** GitHub 저장소로 올린다.
2. 텔레그램 봇을 만든다.
   - 텔레그램에서 @BotFather → `/newbot` → 봇 이름 입력 → 봇 토큰을 받는다.
   - 만든 봇과 대화방을 열고 아무 메시지나 한 번 보낸다.
   - 내 PC에서 채팅 ID를 확인한다:
     `TELEGRAM_BOT_TOKEN=봇토큰 python3 monitor.py --telegram-chat-id`
     (또는 브라우저로 `https://api.telegram.org/bot<봇토큰>/getUpdates`를 열고 `"chat":{"id":...}` 값을 찾는다)
3. 저장소 Settings → Secrets and variables → Actions → New repository secret에 등록한다.
   - `COURTLISTENER_TOKEN` 토큰 문자열만 (courtlistener.com 프로필 → Developer Tools)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
4. Actions 탭 → `courtlistener-monitor` → Run workflow로 첫 실행을 한다. 첫 실행은 최근 7일 사건을 기록만 하고 알림은 보내지 않는다. 이후 하루 두 번 새 사건만 텔레그램으로 온다. 시각은 `.github/workflows/monitor.yml`의 cron(UTC 기준)에서 바꾼다.

## API 한도
CourtListener 검색 API는 계정당 **시간당 50회**다(2026-11-17 실측 응답 `Rate limit exceeded: 50/hour`). 그래서 당사자명 30개씩 묶어 1회 실행에 3~6회만 요청한다. 하루 두 번 실행이면 하루 10회 안팎만 써서 감시 기업을 크게 늘려도 여유가 있다.

## 로컬 실행
```
COURTLISTENER_TOKEN=xxxx python3 monitor.py --since 2026-11-01 --no-notify
```

## 한계
- CourtListener의 연방 사건 데이터(RECAP)는 각 법원 PACER RSS와 RECAP 이용자 업로드로 채워진다. 법원·사건에 따라 반영이 늦거나 빠질 수 있다.
- 주(州)법원, ITC 337조 조사, PTAB(특허심판원) 사건은 대상이 아니다.
- 이름 매칭이라 오탐(동명 회사·자회사 소액 분쟁)이 섞인다. 기사화 전 소장 원문으로 확인한다.
