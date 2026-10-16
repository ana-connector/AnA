# v0.2.0-alpha.1 연결·CI·출시 판정 계획

상태: **1~4단계 완료, 출시 판정은 사용자 몫**. 주소 계산은 한 곳으로 모였고, CI 정의가 들어왔으며, 브라우저 점검과 성능 측정을 마쳤다. 차단 문제는 0건이다. 실제 태깅·배포는 하지 않았다.

## 실행 기록 (2026-10-16, 후보 커밋 `3d72264`)

### 1·2단계. 주소 계산과 전송 경계

[`src/lib/urls.ts`](../../../apps/web/src/lib/urls.ts)가 HTTP와 WebSocket 주소를 한 규칙으로 계산한다. 소켓은 API 기준 주소에서 파생되므로 둘이 호스트를 두고 어긋날 수 없다. 절대 override가 최우선이고, 현재의 3005/8100 분리 기본값은 그대로이며, `NEXT_PUBLIC_SERVER_MODE=same-origin`이 미래의 같은 출처 배포용으로 들어왔다(기본값 아님). [`tests/urls.test.ts`](../../../apps/web/tests/urls.test.ts) 9개가 `node --test`로 각 모드·https·비표준 포트·경로 접두사·IPv6를 고정한다.

직접 `fetch`를 하던 화면 여섯 개는 [api.ts](../../../apps/web/src/lib/api.ts)의 명명된 함수로 옮겼다. 네 가지로 갈려 있던 오류 처리는 `ApiError`(status + FastAPI `detail`) 하나로 모았고, 시크릿 화면의 422 분기와 사용량 편집기의 서버 문구는 그대로 남았다. `rg 'fetch\(|new WebSocket\(|NEXT_PUBLIC_'`는 이제 `api.ts`·`urls.ts` 밖에서 `useChatSocket.ts`의 `new WebSocket(url)` 한 줄만 찾는다.

### 3단계. CI

[.github/workflows/ci.yml](../../../.github/workflows/ci.yml)에 필수 작업 다섯 개를 뒀다. `continue-on-error`는 없다.

| 작업 | 하는 일 |
| --- | --- |
| server-tests | Python 3.13, 파일별 pytest, 모든 종료 코드 집계, 끝나고 `ana-test-pg-*` 잔해 0 확인 |
| web-checks | Node 20에서 `npm ci` → `next typegen` → `tsc --noEmit` → `lint` → `build` |
| address-rules | `node --test apps/web/tests/urls.test.ts` |
| browser-checks | `scripts/e2e-stack.sh check` — 일회용 DB + 진짜 서버 + 대역 CLI + dev 서버 |
| images | 서버·웹 이미지 빌드. 공개 다운로드 실패를 제품 실패와 분리하려고 별도 작업으로 둔다 |

### 4단계. 브라우저·접근성·성능

| 검사 | 결과 |
| --- | --- |
| 서버 테스트 61개 파일 | **893 통과 / 0 실패**, 컨테이너 잔해 0 |
| 주소 규칙 | **9 통과** |
| 웹 정적 검사 | `eslint` 0, `tsc --noEmit` 0, `npm run build` 성공 |
| 브라우저 51개 항목 | **전부 통과** (`scripts/e2e-stack.sh check`, 콜드 스택에서) |
| axe | 밝음·어두움 × 6개 화면 serious·critical **0건** |
| 이미지 빌드 | `ana-server:alpha1-check`, `ana-web:alpha1-check` 둘 다 성공 |
| 출처 규칙 | 허용 출처에만 CORS 헤더, 모르는 출처의 WebSocket은 1008 거절 |
| 데이터 보존 | 보관 → 행·폴더·턴 수 유지, 보관 해제 → 원복. 새로고침 후 대화 유지 |

긴 대화 성능 — 같은 스레드에 턴 400개를 넣고 1280×900 Chromium, dev 서버로 측정했다. 기록해 두는 기준선이며, 같은 절차로 다시 재면 비교할 수 있다.

| 측정 | 값 |
| --- | --- |
| 브라우저가 그리는 메시지 수 | 100개 (서버가 최근 50턴만 돌려준다. 수백 턴이 한 번에 마운트되지 않는다) |
| 첫 로드 → 조작 가능 | 4.70초 (dev 서버의 최초 컴파일 포함) |
| 새로고침 → 100개 그려짐 | 1.36 ~ 1.47초 (3회) |
| 입력 한 글자 → 화면 반영 | 중앙값 33ms, p95 34ms, 최대 38ms |
| 위로 읽는 중 위치 흔들림 | 1.5초 동안 0px |
| 12000px 스크롤 | 503ms, 멈춤 없음 |

### 5단계. alpha.1 출시 판정

| 형제 계획 | 근거 | 판정 |
| --- | --- | --- |
| [01 환경](01-environment-PLAN.md) | venv·uvx 복구, 일회용 DB 픽스처, 수집 재현 | 통과 |
| [02 안정화](02-stability-PLAN.md) | 서버 결함 6건 수정 + 회귀 검사, 893 통과 | 통과 |
| [03 디자인](03-design-PLAN.md) | 방향 B 선택, 토큰 확정, axe 0건 | 통과 |
| [04 UI 독립](04-ui-independence-PLAN.md) | shadcn 실행 의존 0, primitive 자체 구현, 화면 적용 | 통과 |
| 05 이 문서 | 주소 계약, CI, 브라우저·성능 측정 | 통과 |

**차단 문제: 0건.** 아래는 알려진 비차단 사항이며, 출시 노트에 그대로 적는다.

- 실제 Claude·Codex·Kimi 계정의 거절 문구와 페일오버는 검증하지 않았다. 대역 CLI와 fake 어댑터 테스트로만 확인했다.
- 스크린리더 실측, 200% 확대, 실기기 모바일은 하지 않았다. axe와 키보드 조작 검사까지다.
- 같은 출처 호스팅은 함수 단위로만 확인했다. 실제 데몬 배포는 v0.3 범위다.
- Discord 브리지는 서버 테스트 범위이고 실제 봇으로 확인하지 않았다.

실제 태그·이미지 배포는 하지 않았다. 이 표는 "후보가 게이트를 통과했다"는 기록이고, 출시 여부는 사용자가 정한다.

### 되돌리기

- 이 단계의 변경은 전부 웹·테스트·CI·문서다. DB 스키마와 저장 형식은 바뀌지 않았으므로 이전 이미지로 되돌려도 데이터는 그대로다.
- 서버 쪽 변경(턴 종료·중지·게이트)은 프로토콜을 늘리기만 했다(`prompt_ack`, `client_id`). 구버전 브라우저는 `client_id`를 보내지 않고, 서버는 그 경우 ack를 보내지 않으므로 기존 동작 그대로다.
- 화면을 되돌리려면 `ebe5001` 하나를 되돌리면 된다. 토큰과 레이아웃이 그 커밋에 모여 있다.

### alpha.1 릴리스 노트 초안

> **v0.2.0-alpha.1**
>
> *고친 것*
> - 답을 내놓지 못하고 끝난 CLI 턴이 영영 "실행 중"으로 남던 문제. 이제 실패로 처리되어 다음 모델로 넘어가고, 모두 실패하면 오류로 끝난다.
> - 턴이 실패해도 이미 나온 부분 답변을 버리지 않는다.
> - 계획 단계가 모두 소진되면 대화 전체가 끊기던 문제. 이제 일반 답변으로 이어간다.
> - 중지할 때 미결 승인·질문을 정리한다. 중지한 턴의 승인에 나중에 답해 감사 로그가 남는 일이 없다.
> - 실행 중인 턴이 없을 때의 중지가 대기열까지 비운다.
> - 브라우저가 보낸 질문 답변의 개수와 필수 항목을 서버가 검사한다.
> - 전송·승인·질문이 서버 확인 뒤에 반영된다. 연결이 끊기면 입력과 카드가 남아 다시 보낼 수 있다.
> - 한국어 입력기에서 마지막 조합 Enter가 전송되던 문제.
>
> *바뀐 것*
> - 대화 화면이 문서처럼 한 칸으로 읽힌다. 승인·질문은 흐름 안의 삽입 블록이 되고, 사용량은 접힌 한 줄이 된다. 좁은 화면에서는 답해야 할 것이 맨 위에 고정된다.
> - `@shadcn/react` 실행 의존이 사라졌다. 외형은 ANA가 소유한다.
> - 서버 주소 계산이 한 곳으로 모였다. 분리 포트 기본값은 그대로이고, 같은 출처 모드는 설정으로 열어 뒀다.
>
> *알려진 제약*
> - 실제 CLI 계정의 페일오버, 스크린리더, 200% 확대, 실기기 모바일은 검증 범위 밖이다.
> - 데몬 단일 출처 호스팅과 네이티브 셸은 v0.3 대상이다.

---

아래는 작업 전에 작성한 원래 계획이다.

## 01·02단계에서 받은 검증 결과 (2026-10-12)

[02-baseline-RESULTS.md](02-baseline-RESULTS.md)의 최초 서버 기준선은 파일별 순차 실행으로 수집 872 / 통과 860 / 실패 12 / 오류 0이다. 이후 01의 일회용 DB 픽스처와 02의 제품·테스트 수정을 반영했다. 최신 전체 재검증 결과는 [02-stability-PLAN.md](02-stability-PLAN.md)에 기록한다. 단일 pytest 프로세스는 약 87%에서 OOM 종료한 적이 있으므로 CI도 파일별 종료 코드를 모두 집계해야 한다. `memory.peak=6291202048`, `memory.max=max`, `oom_kill=1`로 확인했으며 6.3 GB를 설정된 제한으로 단정하지 않는다.

CI에서는 서버 테스트와 큰 이미지·웹 빌드를 동시에 실행하지 않고, 일회용 `ana_test` DB만 사용한다. 이 문서의 주소 계산·CI·배포 단계는 아직 구현 전이다.

Plan02의 후속 릴레이 수정 뒤 최신 서버 파일별 합산은 61개 파일에서 890개 통과, 실패·오류·건너뜀 0이다. 바뀐 `test_runners.py`의 19개를 재실행하고 나머지 60개 파일의 직전 결과를 재사용했다. 웹 린트도 다시 통과했다. 실제 브라우저·한국어 IME·Kimi 계정 검증과 서버 수신 확인 프로토콜은 아직 없어 02 완료나 출시 판정으로 간주하지 않는다.

## 목표와 경계

- 브라우저에서 API·WebSocket 주소를 한곳에서 계산하고, 현재의 웹 3005/API 8100 분리 배포를 유지하면서 향후 같은 출처 배포에서도 올바른 주소를 만들 준비를 한다.
- 서버 테스트, 웹 타입 검사·린트, 두 Docker 이미지 빌드를 재현 가능한 CI 필수 검사로 만들고 결과를 alpha.1 출시 판정에 연결한다.
- 핵심 브라우저 흐름, 키보드 접근성, 긴 대화의 입력·스크롤 성능을 실제 환경에서 확인하고 증거를 남긴다.
- 범위는 [제품 로드맵](../v1.0.0-product-roadmap.md)의 **v0.2.0-alpha.1**이다. 제품의 최종 구조는 데몬이 호스팅하는 **하나의 웹 UI**이며 Windows·Debian·Android는 그 UI를 여는 얇은 셸이다.

## 범위 밖

- 데몬의 실제 UI 정적 파일 제공, 단일 출처 배포 전환, 서버 인증·기기 페어링, 네이티브 셸 구현과 배포. 이들은 v0.3 이후 단계의 소유다.
- [02-stability-PLAN.md](02-stability-PLAN.md)가 맡는 제품 안정화·기존 ESLint 오류 수정이나 [04-ui-independence-PLAN.md](04-ui-independence-PLAN.md)가 맡는 UI 컴포넌트 전환의 중복 구현.
- 개발용 Compose를 운영 배포로 선언하거나, CI 테스트에 실제 사용자 DB·CLI 자격 증명·운영 볼륨을 사용하는 일.

## 현재 코드에서 확인한 사실과 미확인 항목

1. [apps/web/src/lib/api.ts](../../../apps/web/src/lib/api.ts)의 `apiBase()`는 `NEXT_PUBLIC_API_URL`이 없으면 페이지 호스트의 8100 포트를, `wsBase()`는 `NEXT_PUBLIC_WS_URL`이 없으면 같은 호스트·포트의 `/ws/chat`을 쓴다. 페이지 프로토콜에 따라 HTTP와 WS의 보안 스킴을 고른다. 두 함수의 환경 변수와 기본 동작은 완전히 같은 설정 체계가 아니다.
2. [apps/web/src/hooks/useChatSocket.ts](../../../apps/web/src/hooks/useChatSocket.ts)는 `wsBase()` 뒤에 `workspace` 질의를 붙여 WebSocket을 연다. 주소 함수의 반환 형식이 바뀌면 질의 문자열 처리와 재연결 흐름도 확인해야 한다.
3. `fetch(`를 포함한 UI 파일은 공유 모듈 외 여섯 개다: [secrets/page.tsx](../../../apps/web/src/app/secrets/page.tsx), [tools/page.tsx](../../../apps/web/src/app/tools/page.tsx), [capability-panel.tsx](../../../apps/web/src/components/capability-panel.tsx), [routing-editor.tsx](../../../apps/web/src/components/routing-editor.tsx), [usage-bar.tsx](../../../apps/web/src/components/usage-bar.tsx), [usage-estimate-editor.tsx](../../../apps/web/src/components/usage-estimate-editor.tsx). 현재 호출은 `apiBase()`를 쓰지만 오류 검사·응답 처리 방식은 파일별로 다르다. 이 목록은 현 소스 검색 결과이며 구현 전에 다시 확인한다.
4. [docker-compose.yml](../../../docker-compose.yml)은 웹을 3005, 서버를 8100에 공개한다. 웹 빌드에는 `NEXT_PUBLIC_WS_URL`과 `NEXT_PUBLIC_WS_PORT`를 넘기지만 `NEXT_PUBLIC_API_URL`은 넘기지 않는다. [apps/web/Dockerfile](../../../apps/web/Dockerfile)도 앞의 두 값만 빌드 인자로 선언한다. Next의 공개 환경 값은 빌드 산출물에 포함되므로 주소 설정 방식 변경은 이미지 재사용성을 함께 검토해야 한다.
5. [apps/server/app/main.py](../../../apps/server/app/main.py)의 HTTP CORS와 [apps/server/app/websocket.py](../../../apps/server/app/websocket.py)의 WS Origin 검사는 허용 출처를 공유한다. Compose의 `ANA_ALLOWED_ORIGINS` 기본값은 웹 3005 출처 목록이다. 분리 포트 실사용 검사에서 CORS·WS 1008 거부를 함께 확인해야 한다.
6. [apps/web/package.json](../../../apps/web/package.json)은 `build`와 `lint` 스크립트를 제공하지만 별도 `typecheck`나 브라우저 테스트 스크립트는 없다. [apps/web/tsconfig.json](../../../apps/web/tsconfig.json)은 `noEmit`이다. [README.md](../../../README.md)의 서버 검증 명령은 `.venv/bin/python -m pytest`이며 [pyproject.toml](../../../pyproject.toml)은 `apps/server/tests`를 수집한다.
7. 현재 `.github/workflows` 파일은 보이지 않는다. [apps/server/Dockerfile](../../../apps/server/Dockerfile)은 Python 의존성과 외부 CLI를 설치하고, 웹 Dockerfile은 `npm ci` 후 Next 빌드를 수행한다. CI는 이 외부 다운로드 실패와 코드 실패를 구별해 기록해야 한다.
8. 이전 작업자의 “TypeScript 통과, ESLint `set-state-in-effect` 13건, 비 DB 테스트 80개 통과, 전체 872개 수집, DB 포트 접속 거부”는 **재검증 전 전달 정보**다. 이 문서를 쓰며 해당 검사나 Docker 빌드를 실행하지 않았다.

## 실행 순서

### 1. 브라우저 주소 규칙을 문서와 작은 함수로 고정

- 입력: 현재 `apiBase()`·`wsBase()`, 분리 포트 Compose 값, [01-environment-PLAN.md](01-environment-PLAN.md)의 테스트 실행 환경.
- HTTP와 WS의 기준 출처를 결정하는 순수 계산을 한곳에 모은다. 명시한 API/WS 전체 주소가 있으면 이를 우선하고, 현재 분리 포트 기본값도 유지한다. 향후 같은 출처 모드에서는 페이지의 `location.origin`과 그에 대응하는 `ws:`/`wss:` 출처를 사용하도록 옵션을 정의한다. 같은 출처 모드를 현재 Compose 기본값으로 바꾸지는 않는다.
- 절대 주소·경로 끝 슬래시·`/ws/chat`·기존 질의 문자열을 정규화한다. URL 호스트에는 페이지의 `hostname`만 쓰는 현재 동작을 보존해야 하는지, 비표준 포트·IPv6에서 `URL` 객체로 조립하는 편이 안전한지 확인한다.
- 빌드 인자와 문서의 변수 이름을 함수의 계약과 맞춘다. 분리 배포의 3005/8100 기본 경로와 명시 override가 이미지 빌드 시점에 어떻게 적용되는지 적는다.
- 출력: 한 곳의 주소 계산 계약, 필요한 최소 코드 수정, 분리 포트와 같은 출처 모드의 예시 URL 표.
- 완료 기준: `http://localhost:3005`와 LAN 웹 주소에서 API가 해당 호스트의 8100, WS가 `/ws/chat`으로 향한다. `https:` 페이지의 같은 출처 모드는 HTTPS/WSS를 만든다. 명시 override와 `workspace` 질의가 깨지지 않는다.

### 2. 직접 `fetch` 호출 목록을 전송 경계로 정리

- 입력: 1단계 주소 함수와 여섯 UI 파일, 공유 [api.ts](../../../apps/web/src/lib/api.ts).
- 구현 시작 시 `fetch(`·`new WebSocket(`·`NEXT_PUBLIC_` 사용처를 다시 열거한다. 여섯 파일의 API 요청을 공유 모듈의 명명된 함수 또는 얇은 공통 요청 함수로 옮겨 URL 조립, 실패 상태와 JSON 처리 규칙을 일치시킨다. 화면의 데이터 변환과 표시 상태는 해당 화면에 남긴다.
- 비 API 외부 요청이 발견되면 목록에 별도로 표시하고 API 기준 주소를 억지로 적용하지 않는다. 비밀 값 편집 요청, PATCH/DELETE, 빈 본문 응답, 요청 취소 여부를 실제 호출별로 확인한다.
- 출력: 직접 호출 목록과 호출별 이동 결과, UI에서 서버 출처를 직접 합성하지 않는 전송 경계.
- 완료 기준: API/WS 주소를 바꾸는 데 한곳의 계약만 수정하면 되며, 비밀·도구·역량·라우팅·사용량 화면의 읽기/쓰기 동작과 오류 표시가 유지된다.

### 3. CI 검사 파이프라인 구성

- 입력: [01-environment-PLAN.md](01-environment-PLAN.md)의 일회용 DB 접속 방식과 전체 테스트 기준선, [02-stability-PLAN.md](02-stability-PLAN.md)의 제품 실패 및 린트 수정 결과.
- 저장소의 CI 설정에 Python 3.13 설치·요구사항 설치·파일별 순차 서버 테스트(모든 종료 코드 집계), Node 20에서 `npm ci`·`npx next typegen`·`npx tsc --noEmit`·`npm run lint`·`npm run build`, 서버/웹 Docker 이미지 빌드를 명시한다. 단계별 로그와 종료 코드를 남기고 실패를 숨기는 `continue-on-error`를 출시 게이트에 쓰지 않는다.
- 단일 pytest 프로세스에서 발생한 메모리 누적의 정확한 원인은 별도 조사로 남긴다. 원인이 밝혀지기 전까지 CI는 파일별 독립 프로세스를 쓴다.
- 테스트는 실행 환경의 `ANA_*` 변수를 읽지 않는다. conftest의 `forget_ambient_ana_config`가 상속된 값을 지우므로, CI 실행기에 ANA 설정이 있어도 결과가 바뀌지 않아야 한다.
- 서버 테스트 픽스처의 일회용 `pgvector` 컨테이너가 CI 실행기에서 실제로 접속되는지 먼저 확인한다. 픽스처는 컨테이너 안에서는 같은 Docker 네트워크로, 호스트에서는 `127.0.0.1` 공개 포트로 접속한다. 데몬에 닿지 못하는 실행기는 `ANA_TEST_DATABASE_URL`로 `ana_test` DB를 직접 지정할 수 있다. 개발용 Compose DB나 운영 데이터로 우회하지 않는다. 이미지 빌드에는 실제 계정 토큰을 넣지 않으며 Dockerfile의 공개 다운로드가 실패하면 그 원인을 분리해 기록한다.
- 문서 전용 변경이든 제품 변경이든 alpha.1 후보 커밋에서 동일한 필수 검사를 실행한다. CI 제공자는 저장소의 기존 협업 흐름에 맞춰 정하고, 실행 명령 자체는 로컬에서 재현 가능하게 유지한다.
- 출력: CI 정의, 재현 명령, 실패 로그 접근 경로와 각 작업의 통과/실패 기록.
- 완료 기준: 깨끗한 체크아웃에서 모든 필수 작업이 녹색이며 테스트 픽스처가 종료 후 정리된다. 2단계 소유의 기존 린트 오류가 남으면 출시를 통과시키지 않고 소유 단계로 되돌린다.

### 4. 브라우저 흐름·접근성·성능 확인

- 입력: [03-design-PLAN.md](03-design-PLAN.md)의 상태·포커스 규칙, [04-ui-independence-PLAN.md](04-ui-independence-PLAN.md)의 적용 화면, 안정화된 서버 동작.
- 실제 분리 포트 배포에서 새 대화·스트리밍·중지·에이전트 전환·승인·질문·보관/복원을 순서대로 확인한다. 페이지 새로고침과 WS 재연결 뒤에도 기존 대화가 유지되는지, 개발자 도구에 CORS 실패·WS 1008·잘못된 출처 호출이 없는지 기록한다.
- 채팅·입력창·사이드바·보관 목록·승인·질문·작업 카드를 키보드만으로 탐색한다. Tab 순서, 보이는 포커스, 대화상자 열기/닫기·포커스 복귀, 입력 라벨·오류 안내, 스크린리더의 상태 발표를 수동 점검하고 자동 접근성 검사에서 중대한 위반 0건을 요구한다.
- 긴 대화 샘플과 긴 입력을 정해 동일 브라우저·장치·샘플 크기로 입력 반응, 스크롤 추종/수동 스크롤 유지, 스트리밍 중 누락·중복·점프 여부를 측정한다. 착수 시 기준선과 허용 기준을 기록하고 후보 빌드에서 같은 절차로 재측정한다. 성능 저하가 있으면 수치와 재현 절차를 남겨 수정 후 다시 측정한다.
- 같은 출처 주소 계산은 함수 검사와 격리된 브라우저 시뮬레이션으로 확인한다. 실제 데몬 호스팅이나 프록시 배포가 아직 없으므로 이를 실서비스 단일 출처 통합 검증으로 표시하지 않는다.
- 출력: 브라우저/OS·주소·빌드 식별자·통과/실패·스크린샷 또는 로그·성능 측정값이 붙은 점검표.
- 완료 기준: alpha.1 핵심 흐름과 접근성 항목이 모두 통과하고, 긴 대화에서 입력 손실·스크롤 회귀가 없으며 성능이 기록한 허용 기준 안에 든다.

### 5. alpha.1 출시 판정과 되돌림 준비

- 입력: 1~4단계 산출물과 모든 형제 계획의 완료 근거.
- [01-environment-PLAN.md](01-environment-PLAN.md)의 환경·DB 테스트 접속, [02-stability-PLAN.md](02-stability-PLAN.md)의 실패 테스트와 린트, [03-design-PLAN.md](03-design-PLAN.md)의 디자인 결정, [04-ui-independence-PLAN.md](04-ui-independence-PLAN.md)의 핵심 화면 적용을 한 표에 모은다. 기능 검증과 CI의 실제 후보 커밋 식별자가 같아야 한다.
- 미해결 차단 문제, 알려진 비차단 문제, 데이터·설정 호환성, 이미지 재빌드 및 이전 이미지로의 복귀 절차를 기록한다. 기존 데이터 유지 여부를 직접 확인하고 불확실하면 출시 판정을 보류한다.
- 출력: 통과/실패/해당 없음과 증거 링크가 있는 출시 판정표, 변경·제약·복귀 절차가 적힌 alpha.1 릴리스 노트 초안.
- 완료 기준: 필수 검사·브라우저 점검·데이터 유지가 모두 확인되고 차단 문제가 0건일 때만 alpha.1 후보를 승인한다. 문서 작성 자체는 출시 승인이 아니다.

## 실행 예정 검증 명령

```bash
(failed=0; for file in apps/server/tests/test_*.py; do .venv/bin/python -m pytest "$file" -q || failed=1; done; exit "$failed")
(cd apps/web && npm ci && npx next typegen && npx tsc --noEmit && npm run lint && npm run build)
docker build -f apps/server/Dockerfile -t ana-server:alpha1-check .
docker build -f apps/web/Dockerfile -t ana-web:alpha1-check apps/web
```

- 명령은 **실행 계획**이다. 이 문서 작업에서 통과를 주장하지 않는다. 실제 CI는 각 명령을 독립된 작업/단계로 실행해 어느 검사가 실패했는지 보이게 한다.
- Docker 빌드 성공만으로 분리 포트 브라우저 동작, 같은 출처 운영, 외부 CLI 인증, 데이터 보존이 증명되지 않는다. 해당 증거는 4~5단계에서 별도로 남긴다.

## 형제 계획과 의존 순서

- [01-environment-PLAN.md](01-environment-PLAN.md) → [02-stability-PLAN.md](02-stability-PLAN.md): 서버 테스트 기반과 제품 실패 기준선을 먼저 확정한다. CI 골격과 주소 조사 작업은 이 둘과 병행할 수 있으나 통과 판정은 그 결과를 기다린다.
- [03-design-PLAN.md](03-design-PLAN.md)는 1·2단계와 병행할 수 있다. [04-ui-independence-PLAN.md](04-ui-independence-PLAN.md)의 UI 적용·회귀 검증은 디자인 결정과 안정된 동작을 입력으로 삼는다.
- 이 [05-release-PLAN.md](05-release-PLAN.md)의 alpha.1 출시는 1·2·4단계의 검증 근거를 모두 받은 뒤에 판정한다. 3단계의 결정과 접근성 규칙도 브라우저 검사에 반영한다.

## 위험과 되돌리기

- 주소 규칙을 너무 일찍 같은 출처로 바꾸면 현재 3005 화면이 API를 3005로 호출한다. 분리 포트 기본값을 테스트로 고정하고 모드 전환을 명시적 설정으로 둔다. 이상 시 주소 함수와 빌드 인자를 이전 상태로 되돌린다.
- Next 공개 환경 값이 빌드에 박히면 다른 호스트에 같은 이미지를 옮길 때 잘못된 주소를 가리킬 수 있다. 호스트 파생 기본값을 우선 검증하고 절대 URL override의 적용 범위와 이미지 재빌드 조건을 문서화한다.
- 공통 요청 함수로 옮기며 응답 오류 처리나 `workspace` 질의가 달라질 수 있다. 변경 전후 호출 목록과 HTTP/WS 시나리오를 비교해 회귀 시 작은 단위로 되돌린다.
- CI의 Docker 포트·외부 패키지 다운로드 문제는 제품 실패처럼 보일 수 있다. 일회용 DB와 다운로드 단계의 로그를 분리하되 필수 검사 실패를 무시하지 않는다. CI 변경만 되돌려도 제품 동작과 데이터가 바뀌지 않도록 한다.
- 긴 대화 샘플이 달라지면 성능 비교가 무의미하다. 샘플·장치·브라우저를 기록하고 같은 조건으로 재검증한다. 데이터 변경이 포함되면 이전 이미지 복귀만으로 DB가 복구된다고 가정하지 않는다.

## 사용자에게만 남길 결정

- 현재 필수 결정은 없다. CI 플랫폼이나 alpha.1 배포 채널이 저장소 권한·비용·운영 정책에 걸리면 실제 선택지와 영향이 확인된 뒤 사용자에게만 묻는다. 테스트용 사용자 DB·실제 자격 증명 사용은 선택지에 넣지 않는다.

## 바로 시작할 첫 작업

`apiBase()`·`wsBase()`와 Compose 빌드 인자를 기준으로 **현재 분리 포트와 미래 같은 출처의 주소 표**를 작성하고, `fetch(`·`new WebSocket(` 호출 목록을 다시 검색한다. 그 표를 입력으로 작은 주소 계산 계약부터 구현한다.
