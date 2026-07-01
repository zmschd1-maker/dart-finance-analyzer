# DART 재무제표 분석기 — 포트폴리오 배포 가이드

## ⚠️ 가장 먼저 할 일: API 키 재발급
`dart_10.py` 원본 파일에는 DART / KIS / 네이버 / Gemini 키가 소스코드에 그대로 적혀 있었습니다.
이 파일이 어디든(카카오톡 전송, 개인 클라우드, 지금 이 대화 업로드 등) 한 번이라도 노출된 적이
있다면, **기존 키는 이미 유출된 것으로 간주**하고 아래 키를 전부 재발급 받으세요.
재발급 없이 그대로 쓰면, 새로 만든 안전한 버전이 무의미해집니다.

- DART: https://opendart.fss.or.kr → 마이페이지 → 인증키 재발급
- KIS(한국투자증권): 홈페이지 → Open API 신청 화면에서 앱키/시크릿 재발급.
  **이때 반드시 실전 계좌 키라면, 조회(시세) 권한만 있는지 확인하세요.**
  이 코드는 매수/매도 주문 기능은 쓰지 않지만, 키 자체에 주문 권한이 있으면 위험도가 올라갑니다.
- 네이버 검색 API: 네이버 개발자센터 → 애플리케이션 재등록
- Gemini: Google AI Studio → API 키 재발급

## 1. 로컬에서 먼저 테스트
```bash
cd dart_deploy
cp .env.example .env
# .env 파일을 열어 실제 키 값으로 채워넣기
# (아래는 macOS/Linux 기준. Windows는 setx 또는 PowerShell 사용)
export $(cat .env | xargs)
python app.py
```
브라우저에서 http://localhost:8787 접속해 기존과 동일하게 동작하는지 확인합니다.

## 2. GitHub에 올리기 (⚠️ .env는 절대 커밋 금지)
```bash
git init
git add app.py Procfile requirements.txt .gitignore .env.example
git commit -m "portfolio: DART finance analyzer (env-based keys)"
git remote add origin <본인 GitHub 저장소 URL>
git push -u origin main
```
`.gitignore`에 `.env`가 포함되어 있어 실수로 키가 커밋되는 걸 막아줍니다.
커밋 전에 `git status`로 `.env`가 목록에 없는지 한 번 더 확인하세요.

## 3. Railway 배포
1. https://railway.app 접속 → GitHub 계정으로 로그인
2. "New Project" → "Deploy from GitHub repo" → 방금 올린 저장소 선택
3. 배포가 시작되면 프로젝트 화면에서 **Variables** 탭 클릭
4. 아래 6개 환경변수를 하나씩 추가 (Key/Value 입력):
   - `DART_API_KEY`
   - `KIS_APP_KEY`
   - `KIS_APP_SECRET`
   - `NAVER_CLIENT_ID`
   - `NAVER_CLIENT_SECRET`
   - `GEMINI_API_KEY`
5. Variables 저장하면 자동으로 재배포됩니다 (재배포 안 되면 "Redeploy" 버튼 클릭)
6. **Settings → Networking → Generate Domain** 클릭 → `xxxx.up.railway.app` 형태의 공개 URL 생성
7. 그 URL로 접속해 정상 동작 확인

Railway는 `PORT` 환경변수를 자동으로 주입합니다. 코드에서 이미 `os.environ.get('PORT')`를
읽어 호스트를 `0.0.0.0`으로 바꾸도록 처리해뒀으니 별도 설정 불필요합니다.

## 4. 자기소개서에 링크 넣기
- Railway가 준 URL을 그대로 쓰거나, Settings에서 커스텀 도메인 연결 가능
- 자소서/이력서에는 텍스트 링크보다 "포트폴리오 보기 →" 같은 앵커 텍스트 + URL을 함께 적는 걸 추천
- 노션이나 개인 사이트가 있다면, 거기에 스크린샷 + 링크 + 간단한 기술 설명(사용 API, 구조)을 얹은
  소개 페이지를 하나 만들고 그 페이지를 자소서에 링크하는 방법도 좋습니다 (배포 URL이 죽어도
  스크린샷은 남아있으니 이중 안전장치가 됨)

## 5. 배포 후 반드시 확인할 것
- [ ] 무료 요금제(Railway Free/Hobby)의 월 사용 시간·크레딧 한도를 확인 (면접 시즌에 갑자기 죽지 않도록)
- [ ] 서버가 잠들었다가 첫 요청에 느리게 깨어나는 슬립 모드가 있는지 확인 → 있다면 면접 직전에
      미리 한 번 접속해서 깨워두기
- [ ] KIS API는 하루 호출 한도가 있습니다. 면접관 여러 명이 동시에 눌러도 문제없도록
      이번에 넣은 10분 캐시가 정상 작동하는지 화면 새로고침으로 확인
- [ ] 만약 잦은 사용으로 API 한도가 걱정되면, 화면 상단에 "실시간 데이터는 10분 캐시로 제공됩니다"
      정도의 안내 문구를 하나 추가하는 것도 신뢰도 있어 보입니다

## 이번에 코드에 추가된 안전장치 요약
1. **API 키 하드코딩 제거** → 전부 환경변수로 이동
2. **10분 캐시** → 같은 요청은 외부 API를 다시 호출하지 않고 캐시된 값 반환
3. **IP당 분당 20회 요청 제한** → 특정 사용자가 반복 새로고침해도 외부 API 호출 폭주 방지
4. **클라우드 배포 감지** → `PORT` 환경변수 유무로 로컬 실행/클라우드 배포를 자동 구분해서
   포트·호스트·브라우저 자동 실행 로직을 다르게 처리
5. **필수 키 누락 시 서버 로그에 경고 출력** → 환경변수 설정을 빠뜨렸을 때 바로 알아챌 수 있음
