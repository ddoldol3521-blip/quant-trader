# 웹에 올리기 (Streamlit Community Cloud)

인터넷 주소를 하나 만들어서, PC·폰 아무데서나 접속하고 링크를 아는 사람에게 공유하는 방법입니다. **무료입니다.**

준비물은 GitHub 계정 하나뿐이고, 전부 합쳐 **20~30분** 걸립니다.

---

## 1단계 — GitHub 계정 만들기 (5분)

이미 있으면 건너뛰세요.

1. https://github.com/signup 접속
2. 이메일 · 비밀번호 · 사용자이름(영문) 입력
3. 이메일로 온 인증코드 입력

> 비밀번호는 직접 정하고 직접 입력하세요. 어디에도 적어두지 마세요.

---

## 1.5단계 — 이메일 숨기기 (선택, 2분)

Public 저장소에 올리면 **커밋에 적힌 이메일 주소가 공개**됩니다. 스팸이 걱정되면 GitHub가 주는 가짜 주소로 바꾸세요. **올리기 전에** 해야 합니다.

1. https://github.com/settings/emails 접속
2. **Keep my email addresses private** 체크
3. 바로 위에 표시된 `12345678+사용자이름@users.noreply.github.com` 주소를 복사
4. 아래 명령 실행 (복사한 주소로 바꿔서)

```bash
git config user.email "12345678+사용자이름@users.noreply.github.com"
```

```bash
git filter-branch -f --env-filter 'export GIT_AUTHOR_EMAIL="12345678+사용자이름@users.noreply.github.com"; export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"' -- --all
```

두 번째 명령은 **이미 만들어둔 커밋들의 이메일까지** 바꿉니다. 아직 아무 데도 안 올렸으니 안전합니다.

---

## 2단계 — 저장소(repository) 만들기 (3분)

1. https://github.com/new 접속
2. **Repository name**에 `quant-trader` 입력
3. **Public** 선택
   - 코드가 공개됩니다. 앱 주소는 검색에 걸리지 않으니 링크를 아는 사람만 씁니다.
   - 코드까지 감추고 싶으면 Private을 골라도 됩니다 (Streamlit 무료 플랜에서 비공개 저장소 1개까지 배포 가능).
4. 나머지는 **아무것도 체크하지 말고** 초록색 **Create repository** 클릭

> ⚠️ "Add a README file" 같은 체크박스를 켜면 다음 단계에서 충돌이 납니다. 전부 비워두세요.

만들고 나면 `https://github.com/사용자이름/quant-trader` 주소가 나옵니다. 이 주소를 복사해두세요.

---

## 3단계 — 코드 올리기 (5분)

프로젝트 폴더에서 아래를 순서대로 실행하세요. `사용자이름` 부분만 본인 것으로 바꾸면 됩니다.

```bash
git remote add origin https://github.com/사용자이름/quant-trader.git
```

```bash
git branch -M main
```

```bash
git push -u origin main
```

마지막 명령에서 **로그인 창이 뜹니다.**

이 PC에는 Git Credential Manager가 설치돼 있어서, 보통 아래처럼 진행됩니다.

1. `Connect to GitHub` 창 → **Sign in with your browser** 클릭
2. 브라우저가 열리며 GitHub 로그인 → **Authorize** 클릭
3. 창이 자동으로 닫히고 업로드가 진행됨

한 번 로그인하면 다음부터는 안 묻습니다.

> 브라우저 방식이 안 되고 아이디/비밀번호를 물으면, 비밀번호 자리에는 **일반 비밀번호가 아니라 토큰**이 필요합니다.
> https://github.com/settings/tokens 에서 *Generate new token (classic)* → `repo` 체크 → 생성된 문자열을 붙여넣으세요.

성공하면 이런 메시지가 나옵니다:

```
branch 'main' set up to track 'origin/main'.
```

GitHub 저장소 페이지를 새로고침하면 파일들이 보입니다.

### 올라가지 않는 파일들

아래는 `.gitignore`에 들어 있어 **일부러 제외**됩니다. 개인 정보라 공개되면 안 됩니다.

| 파일 | 내용 |
|---|---|
| `telegram_config.json` | 텔레그램 봇 토큰 |
| `notify_schedule.json` | 알림 예약 설정 |
| `jongsa_settings.json` | 내 시드·시작일 설정 |
| `.venv/` | 파이썬 설치 폴더 (용량 큼) |

올리기 전에 확인하려면:

```bash
git status --short
```

---

## 4단계 — Streamlit Cloud 연결 (5분)

1. https://share.streamlit.io 접속
2. **Continue with GitHub** → 방금 만든 계정으로 로그인 → 권한 허용
3. **Create app** → **Deploy a public app from GitHub** 선택
4. 아래처럼 채웁니다

   | 항목 | 값 |
   |---|---|
   | Repository | `사용자이름/quant-trader` |
   | Branch | `main` |
   | **Main file path** | **`jongsa_app.py`** ← 기본값이 `streamlit_app.py`라 반드시 바꿔야 합니다 |
   | App URL | 원하는 주소 (예: `jongsa-v5`) |

5. **Advanced settings** → **Python version**을 **3.12**로 지정
6. **Deploy** 클릭

3~5분 정도 설치가 돌아가고 나면 `https://정한이름.streamlit.app` 주소로 앱이 뜹니다.

---

## 5단계 — 확인 (2분)

주소에 접속해서 아래를 확인하세요.

- [ ] 총자산·누적수익률 숫자가 나오는가 (데이터를 못 받으면 여기서 멈춥니다)
- [ ] 설정값을 바꾸면 숫자가 다시 계산되는가
- [ ] 폰으로 접속해도 보이는가

제목 밑에 이런 안내가 뜨면 정상입니다:

> 설정은 이 브라우저 탭에서만 유지되고 서버에 저장되지 않습니다 — 새로고침하면 기본값으로 돌아갑니다.

---

## 알아둬야 할 것

### 설정은 저장되지 않습니다

웹 버전은 **접속자마다 설정이 따로** 놀도록 만들었습니다. 서버에 파일 하나로 저장하면 접속한 사람들끼리 서로의 설정을 덮어쓰기 때문입니다.

그래서 **새로고침하면 기본값(SOXL / $10,000 / 10분할 / 2025-01-02)으로 돌아갑니다.** 내 설정을 계속 유지하고 싶으면 PC에서 `run_jongsa.bat`으로 켜는 로컬 버전을 쓰세요. 로컬은 `jongsa_settings.json`에 저장됩니다.

### 앱이 잠들었다가 깨어납니다

무료 플랜은 일주일 정도 아무도 안 들어오면 앱이 잠듭니다. 다음에 접속하면 "Yes, get this app back up!" 버튼이 뜨고, 누르면 1~2분 뒤 다시 켜집니다. 데이터가 사라지지는 않습니다.

### 코드를 고치면 자동으로 반영됩니다

로컬에서 수정하고 아래를 실행하면 몇 분 뒤 웹 앱에도 반영됩니다.

```bash
git add -A; git commit -m "수정 내용"; git push
```

### 분석용 앱(app.py)도 올리고 싶다면

같은 저장소로 **Create app을 한 번 더** 하되, Main file path를 `app.py`로 지정하면 별도 주소가 생깁니다.

단, 그 앱의 **텔레그램 알림 탭은 웹에서 동작하지 않습니다.** 윈도우 작업 스케줄러를 쓰는 기능이라 리눅스 서버에서는 예약이 걸리지 않습니다. 알림은 PC 로컬 버전에서만 쓰세요.

---

## 문제가 생기면

| 증상 | 원인과 해결 |
|---|---|
| `error: remote origin already exists` | 이미 연결돼 있음. `git remote set-url origin 주소`로 바꾸세요 |
| `Updates were rejected` | 2단계에서 README 체크박스를 켰을 때. `git pull --rebase origin main` 후 다시 push |
| 배포 로그에 `ModuleNotFoundError` | `requirements.txt`에 빠진 패키지. 추가하고 push하면 자동 재설치 |
| 숫자 대신 "계산 실패" | 시세 서버가 일시적으로 막힌 경우. 1~2분 뒤 새로고침 |
| 앱이 계속 "Please wait..." | 오른쪽 아래 **Manage app**에서 로그를 열어 빨간 줄을 확인 |

---

## 공개 범위에 대해

- **코드(GitHub)**: Public으로 하면 누구나 볼 수 있습니다. 개인 설정 파일은 제외되므로 시드 금액 같은 건 노출되지 않습니다.
- **앱(Streamlit)**: 주소를 아는 사람은 누구나 들어올 수 있습니다. 검색엔진에 자동 등록되지는 않지만, **비밀번호는 걸려 있지 않습니다.**
- 특정 사람만 들어오게 하려면 Streamlit Cloud 앱 설정의 **Settings → Sharing**에서 이메일을 지정해 제한할 수 있습니다.

이 앱은 계산기이고 **투자 자문이 아닙니다.** 다른 사람에게 공유할 때 이 점을 같이 알려주세요. SOXL은 3배 레버리지 ETF라 손실 폭이 큽니다.
