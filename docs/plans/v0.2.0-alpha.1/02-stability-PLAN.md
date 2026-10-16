# v0.2.0-alpha.1 단계 2 — 핵심 대화 흐름 안정화 계획

상태: **구현 완료**. 2026-10-16 기준 서버 테스트 61개 파일 893개가 모두 통과하고, 웹 `eslint`·`tsc --noEmit`·`npm run build`가 통과하며, 실제 Chromium에서 도는 51개 항목([apps/web/tests/browser](../../../apps/web/tests/browser/README.md))도 통과한다. 남은 미검증은 실제 한국어 IME 조합(Playwright가 조합 이벤트를 만들지 못한다)과 실제 Kimi·Claude 계정의 거절·페일오버 두 가지다. 아래의 원래 계획과 가설은 이 실행 기록으로 갱신한다.

## 실행 기록 (2026-10-12, `/home/hello/ana` main 작업 트리)

### 기준선과 환경 (1·2단계)

[02-baseline-RESULTS.md](02-baseline-RESULTS.md)의 최초 실행은 61개 파일에서 수집 872 / 통과 860 / 실패 12 / 오류 0이었다. 2026-10-16 최종 재실행은 61개 파일 893 통과 / 0 실패다. 실행 컨테이너와 호스트 Docker 데몬의 네트워크 차이를 반영한 일회용 `ana_test` DB 픽스처, 상속된 `ANA_*` 격리, 게이트 토큰 전달·해제, 실패 원인 기록, 현행 계약에 맞춘 테스트 수정이 반영됐다. 첫 수정 후 전체 파일별 실행은 871 통과 / 1 실패였고, 그 1건은 토큰 해제 수정 후 같은 파일을 3회 재실행해 통과했다. 이전 전체 결과를 현재 결과처럼 세지 않는다.

### Kimi·전환과 서버 대화 흐름 (3~5단계)

- 출력·오류 없이 끝나는 adapter 스트림은 실패로 처리해 다음 rung의 페일오버가 가능해졌다. 계획 단계의 모든 rung이 실패해도 직접 답변 단계로 이어진다. 실패한 턴은 이미 나온 부분 답변 뒤에 오류 안내를 붙이고, 원인을 트랜스크립트와 turn 메타데이터에 보존한다.
- 명시적 중지는 해당 턴의 미결 승인·질문을 안전한 기본값으로 해결하고 중복된 늦은 응답을 거절한다. 실행 중인 턴이 없으면 대기열의 prompt를 버리고 중지 응답을 돌려준다. runner 종료 도중 취소가 삼켜져 정리가 멈추는 경로도 수정했다.
- 완료·취소 시 재연결 버퍼는 끝난 턴의 프레임을 비우되 아직 실행하지 않은 대기 prompt는 남긴다. 브라우저 질문 응답의 개수와 필수 값은 서버에서 검사한다.
- 예상 밖 턴 예외도 해당 턴의 `error` 프레임으로 끝내고 릴레이는 다음 대기 prompt를 처리한다. 첫 턴이 실패한 뒤 두 번째 턴이 완료되는 회귀 검사를 추가했다.
- `test_kimi_adapter.py`, `test_failover.py`, `test_cancel.py`, `test_gate_registry.py` 및 수정된 graph·runner·gate·WebSocket 테스트를 파일별 독립 프로세스로 재실행해 통과했다. 실제 Kimi 계정의 거절 문구와 CLI 버전별 동작은 검증하지 않았다.

### 웹 응답·보관·긴 대화·린트 (5~7단계)

- 승인·질문 카드는 `WebSocket.send()` 직후 사라지지 않고 서버의 `request_resolved` 프레임을 기다린다. 끊긴 연결에서는 버튼을 막고, 재연결되면 같은 답을 다시 보낼 수 있다. 전송 실패 때 카드와 입력을 유지한다.
- 이미 열린 작업공간을 보관하면 사이드바 보관함을 펼쳐 현재 대화와 복원 경로를 보이게 한다. 서버는 기존 `archived` PATCH로 ID·폴더·기록을 유지한다. 웹 보관·복원 조작은 기존 설정 화면에 있다.
- Composer는 한국어 IME의 마지막 조합 Enter에서 `keyCode=229`를 전송으로 취급하지 않는다. 초안은 composer 로컬 상태에 있고 스크롤은 기존 `MessageScroller`가 맡는다.
- prompt에 브라우저가 만든 `client_id`를 싣고 서버가 `prompt_ack`로 수락 여부를 돌려준다. 입력창은 ack를 받은 뒤에만 비워지고, ack 전에 연결이 끊기면 초안이 남아 같은 `client_id`로 재전송된다. 서버는 이미 받은 `client_id`를 다시 큐에 넣지 않으므로 재전송이 턴을 두 번 돌리지 않는다.
- 턴 하나가 예기치 않게 실패해도 릴레이는 죽지 않는다. 그 턴만 `error` 프레임으로 끝나고 소켓은 열린 채 다음 prompt를 받는다. `test_websocket.py`의 계약도 1011 종료 기대에서 "다음 prompt를 계속 받는다"로 바뀌었다.
- 선행 `set-state-in-effect` 13건은 `6a01d46`에서 원인별로 수정했다. 비동기 로드 상태 소유, `useSyncExternalStore`, 작업공간 ID로 구분한 편집 폼을 사용하며 규칙 비활성화나 일반 훅 은폐는 없다. 이번 통합 변경 후 Node 20.20.2에서 `npm run lint`, `npx next typegen && npx tsc --noEmit`, `npm run build`가 모두 통과했다.

### 아직 검증하거나 고쳐야 할 부분

- ~~이 환경에서 브라우저 실행 파일과 자동화 도구를 찾지 못했다.~~ Playwright와 Chromium이 있었다. 전송·중지·중지 후 재전송, 승인·질문, 보관·복원, 새로고침 후 대화 유지, 360px, 키보드 조작, 긴 대화 성능을 모두 실제 브라우저에서 확인했다([05](05-release-PLAN.md)의 측정표).
- 남은 미검증: 실제 한국어 IME 조합(자동화로 조합 이벤트를 만들 수 없다), 실제 Kimi·Claude 계정의 거절 문구와 페일오버, 스크린리더, 200% 확대, 실기기.
- 답변 단계의 무출력 타임아웃과 실제 CLI의 한도 메시지 분류는 미확인이다.
- 웹은 자동 라우팅을 보내므로 명시적 Kimi 선택의 쿨다운 동작은 웹 UI에서 직접 시험할 수 없다. `ANA_BACKGROUND_TURNS=off`의 동일 대화 다중 탭 소유권도 별도 검증이 필요하다.

## 목표와 경계

- Kimi 실행·페일오버, 전송·중지, 승인·질문, 보관·복원, 긴 대화의 입력·스크롤을 한 작업공간의 실제 사용 흐름으로 검증하고 확인된 결함을 고친다.
- 실패 테스트는 먼저 현재 계약과 기대값을 대조한다. 재현되는 제품 결함은 수정하고, 오래된 기대값은 변경 이유와 근거를 남긴 뒤 테스트를 갱신한다.
- React `set-state-in-effect` 13건이라는 선행 보고를 린트 출력으로 다시 확인하고, 확인된 각 위치의 상태 소유·동기화 원인을 수정한다. 일괄 규칙 비활성화나 일반 훅으로의 은폐는 완료로 보지 않는다.
- 데이터·작업 상태를 보존한다. v0.2.0-alpha.1은 현재 웹 UI의 안정화 단계다. 데몬이 호스팅하는 **하나의 UI**와 Windows·Debian·Android의 얇은 셸은 제품 구조 원칙이지만, 실제 단일 출처 호스팅과 앱 구현은 이 단계의 범위가 아니다. [제품 로드맵](../v1.0.0-product-roadmap.md)을 따른다.

범위 밖: 새 네이티브 화면, 별도 임베디드 UI, 다중 기기 상태 동기화·서버 재시작 후 실행 복구(v0.4), 보관을 삭제로 바꾸는 데이터 정책, UI 전면 재설계.

## 현재 코드에서 확인한 사실

| 흐름 | 관찰한 구현·검증 위치 | 이번 단계에서 확인할 점 |
| --- | --- | --- |
| Kimi·페일오버 | `packages/adapters/kimi.py`는 출력 없는 정상 종료를 오류로 올리고, `packages/orchestrator/breaker.py`는 Kimi 선호 경로와 다음 rung을 정의한다. `apps/server/tests/test_kimi_adapter.py`, `test_failover.py`에 관련 사례가 있다. | CLI 거절·무응답·프로세스 종료 때 마지막 오류와 전환 공지가 한 턴에 정확히 남는가. |
| 전송·중지 | `apps/web/src/hooks/useChatSocket.ts`는 WebSocket 전송 후 즉시 사용자 메시지와 `thinking` 상태를 표시한다. `apps/server/app/websocket.py`는 prompt를 큐에 넣고 cancel은 실행 중인 턴에 적용한다. `apps/server/tests/test_cancel.py`, `test_websocket.py`, `test_runners.py`가 계약 일부를 다룬다. | 전송 실패 시 초안 유지, 중지 후 UI와 서버 상태 일치, 재연결·대기 중인 후속 prompt의 순서. |
| 승인·질문 | `apps/server/app/gate.py`와 `runners.py`가 요청·해결을 다루고, 웹 훅의 `answerQuestion`·`answerApproval`이 카드를 제거한다. `test_gate_registry.py`, `test_ask_user.py`, `test_runners.py`가 일부 경로를 검증한다. | 응답 실패·만료·다른 탭 응답 후 카드가 거짓 성공처럼 사라지지 않는가. |
| 보관·복원 | `packages/orchestrator/workspaces.py`의 `archived` 갱신은 행·폴더·대화를 유지한다. `apps/server/app/main.py`의 PATCH와 `apps/web/src/lib/api.ts`의 `updateWorkspace`가 필드를 받는다. `test_workspaces.py`는 서버 보존을 검증한다. | `session-sidebar.tsx`는 보관 목록을 표시하지만 조사한 UI 호출부에는 `archived`를 보내는 동작이 보이지 않는다. UI에서 보관·복원 가능한지 재현해 확정한다. |
| 긴 대화 | `apps/web/src/components/composer.tsx`가 초안을 로컬 상태로 유지하고 입력 높이를 3줄까지 늘린다. `chat-page.tsx`는 스크롤 영역과 입력창을 분리한다. `ui/message-scroller.tsx`는 메시지 높이 추정으로 인한 이동 오차를 피하려는 코드가 있다. | 긴 기록·스트리밍에서 입력 지연, 스크롤 점프, 하단 고정·수동 탐색, 한국어 조합 입력을 실제 화면으로 확인한다. |
| effect 린트 | `workspace-settings.tsx`는 props에서 복제한 편집 상태를 effect로 재설정하고, `hooks/use-mobile.ts`는 effect 안에서 viewport 값을 즉시 설정한다. `useChatSocket.ts`에는 기록 로드 시작·종료 상태가 있다. | 선행 보고의 13건과 실제 린트 위치를 대조하고 각 원인을 분류한다. 지금 이 문서 작업에서는 린트를 실행하지 않았다. |

위 표는 작업 전 조사다. 보관 UI 부재 가설은 실제 설정 화면의 보관·복원 버튼을 확인해 기각했고, 승인·질문 카드 소실은 수정했다. 긴 대화 성능과 실제 IME 입력은 브라우저 실측 전이므로 검증하지 못했다. 선행 린트 13건은 원인별 수정 후 현재 0건이다.

### 실패를 판정하는 기록 방식

각 실패는 다음 정보를 한 줄씩 남긴다.

- 실행한 커밋, OS/브라우저, Python·Node·CLI 버전과 환경 변수 중 동작에 영향을 준 값.
- 기대한 계약: 로드맵의 alpha.1 완료 조건, 현재 API·WebSocket 형식, 보존해야 할 기존 데이터.
- 실제 결과: 실패한 명령과 테스트명 또는 화면 조작 순서, 서버 프레임 순서, UI 표시.
- 재현 여부: 같은 환경에서 다시 나타나는지, fixture·포트·의존성 문제를 제거한 뒤에도 남는지.
- 분류와 조치: 제품 결함이면 원인 코드와 회귀 검증, 낡은 기대값이면 바뀐 계약의 근거와 테스트 갱신, 환경 문제면 01에 전달.

판정 예시는 아래와 같다. 실제 실패 확인 전에는 모두 예시이며 결과가 아니다.

| 신호 | 제품 결함으로 판정할 조건 | 낡은 기대값 또는 환경 문제로 판정할 조건 |
| --- | --- | --- |
| Kimi 무응답 | CLI가 거절했는데 성공 턴으로 저장되어 다음 rung을 시도하지 않음 | fake CLI가 현행 이벤트 형식과 다른 출력만 생성함 |
| 브라우저 닫힘 | 연결이 끊겼다는 이유로 백그라운드 턴이 유실됨 | 현재 설정에서 의도적으로 `background_turns_enabled()`가 꺼져 있음 |
| 보관 후 목록 | PATCH 성공 뒤 새로고침에서 `archived`가 사라지거나 기록이 유실됨 | 화면에 조작 버튼이 아직 없는 구현 공백이며 기존 DB 계약은 정상 |
| DB fixture | 접속 가능한 DB에서도 저장·조회가 틀림 | `conftest.py`의 공개 포트에 localhost 연결 자체가 실패함 |

## 진행 순서와 산출물

1. **기준선 확보** — [01-environment-PLAN.md](01-environment-PLAN.md)의 Docker·Python·Node 실행 환경과 DB 포트 접근을 먼저 준비한다. 입력: 고정된 커밋, 설치된 의존성, PostgreSQL fixture. 출력: 명령·환경·실패 스택·소요 시간을 적은 기준선 표. `test_kimi_adapter.py`, `test_failover.py`, `test_cancel.py`, `test_runners.py`, `test_workspaces.py`를 각각 좁게 실행하고 ESLint의 모든 위치를 저장한다. DB 접속 실패는 제품 실패로 세지 않는다.
2. **실패 분류** — 각 실패를 재현 가능한 제품 결함, 바뀐 계약에 뒤처진 테스트, 실행 환경 문제로 나눈다. 입력: 기준선과 실제 서버·UI 계약. 출력: 실패별 재현 단계·근거·소유 파일·수정 범위. 예: 연결 종료가 턴을 취소해야 한다는 기대는 `background_turns_enabled()`와 `test_websocket.py`의 현행 계약을 먼저 대조한다. 기대값 수정만으로 실제 회귀를 가리지 않는다.
3. **Kimi·전환 수정** — Kimi fake CLI 사례부터 무응답, 거절 메시지, 비정상 종료, 다른 CLI로의 전환, 전환 후 최종 결과를 묶어 확인한다. 입력: `fake_kimi.py`, adapter 이벤트, breaker chain, turn timeline. 출력: 원인 위치의 최소 수정과 실패 전 재현·수정 후 통과하는 테스트. 오류를 성공으로 저장하거나 같은 요청을 무한 재시도하지 않으며 사용자가 볼 전환 사유를 유지한다.
4. **전송·중지 수정** — WebSocket 열림·닫힘·재연결, 전송 실패, prompt 큐, cancel을 한 턴씩 따라간다. 입력: `useChatSocket.ts`, `websocket.py`, `runners.py`와 기존 cancel·WebSocket 테스트. 출력: 서버 상태와 화면의 실행 표시가 일치하는 수정 및 중지 뒤 다음 전송을 확인하는 회귀 사례. 클라이언트 창 닫힘과 명시적 중지는 구분한다.
5. **승인·질문 수정** — 서버의 request ID와 gate session을 기준으로 응답·만료·다른 탭 해결을 점검한다. 입력: `gate.py`, `runners.py`, `useChatSocket.ts`, `approval-card.tsx`, `question-card.tsx`. 출력: 중복·오래된 응답의 처리 결과와 카드 상태를 확인하는 테스트/수동 시나리오. 브라우저가 전송하지 못한 답은 성공으로 표시하지 않고, 승인 거부·시간 초과의 안전한 기본값을 유지한다.
6. **보관·복원 수정** — 서버 PATCH와 사이드바·작업공간 설정의 실제 호출 경로를 확인한다. 입력: `archived` DB 필드, API, 목록 상태, 현재 작업공간 ID. 출력: UI에서 보관과 복원을 실행하고 새로고침 후에도 목록·기존 대화가 유지되는 흐름. 새 API나 데이터 이동 없이 기존 PATCH를 재사용한다. 삭제 동작과 분리한다.
7. **긴 대화와 effect 린트 원인 수정** — 입력 100회 이상, 긴 응답, 수백 메시지, 스트리밍 중 수동 스크롤을 재현한다. 입력: profiler/브라우저 관찰, 실제 ESLint 위치, `composer.tsx`, `chat-page.tsx`, `message-scroller.tsx`. 출력: 입력 반응·스크롤 동작 기록과 린트 0건. 원인별 방향은 아래와 같다.
   - 로드 시작을 effect에서 동기적으로 `setState`하는 곳은 요청 대상과 결과로 로딩 상태를 표현하고, 비동기 완료 시 갱신한다. 이전 요청의 늦은 응답이 새 작업공간을 덮지 않게 한다.
   - props에서 만든 편집 초안은 작업공간 ID로 컴포넌트 생명주기를 나누거나 실제 선택 변경 핸들러에서 초기화한다. 입력 중인 값을 부모 재렌더만으로 지우지 않는다.
   - viewport 상태는 `matchMedia`의 구독과 현재 스냅샷을 일치시키는 방식(예: `useSyncExternalStore`)으로 읽고 서버 렌더링 값을 명시한다.
   - 나머지 오류도 실제 위치별 상태 소유자를 정해 고친다. `eslint-disable`, 규칙 제거, 간접 호출 래퍼만으로 개수를 0으로 만들지 않는다.
8. **흐름 재검증** — 수정한 범위의 회귀 테스트, 타입 검사, 린트, 브라우저 수동 시나리오를 다시 실행한다. 입력: 2~7의 변경. 출력: 통과/실패/미실행과 근거를 담은 검증표. [03-design-PLAN.md](03-design-PLAN.md)의 시안과 [04-ui-independence-PLAN.md](04-ui-independence-PLAN.md)의 컴포넌트 교체가 시작되기 전에 동작 계약을 전달한다.

## 실행 명령과 합격 기준

환경 준비 후 저장소 루트에서 관련 서버 테스트를 파일마다 독립 프로세스로 실행한다. fixture는 실행 컨테이너에서 같은 Docker 네트워크의 임시 `pgvector/pgvector:pg16`에 접속하고, 호스트 실행기에서만 loopback 포트를 공개한다. 실제 DSN 접속과 `ana_test` 이름을 마이그레이션 전에 확인한다.

```bash
.venv/bin/python -m pytest -q apps/server/tests/test_kimi_adapter.py
.venv/bin/python -m pytest -q apps/server/tests/test_failover.py
.venv/bin/python -m pytest -q apps/server/tests/test_cancel.py
.venv/bin/python -m pytest -q apps/server/tests/test_websocket.py
.venv/bin/python -m pytest -q apps/server/tests/test_runners.py
.venv/bin/python -m pytest -q apps/server/tests/test_gate_registry.py
.venv/bin/python -m pytest -q apps/server/tests/test_ask_user.py
.venv/bin/python -m pytest -q apps/server/tests/test_workspaces.py
(cd apps/web && npm run lint)
(cd apps/web && npx next typegen && npx tsc --noEmit && npm run build)
```

수동 검증에는 임시 작업공간 하나와 긴 기록이 있는 작업공간 하나를 쓴다. 각 시나리오의 서버 프레임과 화면 상태를 함께 남긴다.

| 시나리오 | 관찰할 통과 조건 |
| --- | --- |
| 전송 직후 응답 지연 | 입력은 한 번만 전송되고, 첫 프레임 전에도 진행 상태가 보이며, 소켓 전송 실패에는 초안이 남는다. |
| 실행 중 중지·재전송 | 취소 결과가 한 번 표시되고 CLI/worker가 정리되며, 다음 턴을 같은 작업공간에서 보낼 수 있다. |
| 승인 허용·거부·시간 초과 | request ID별로 한 결정만 적용되고, 최종 결과와 감사 기록이 맞는다. |
| 질문 필수·선택·자유 입력 | 누락된 필수 답은 전송되지 않고, 정상 답과 다른 탭 해결 시 카드가 일치한다. |
| 보관·복원·재접속 | 목록 위치만 변하고 ID·폴더·대화 및 기존 턴 수가 같다. |
| 긴 대화·화면 크기 변경 | 과거 읽기 중 자동 이동이 없고 하단 복귀가 가능하며, 입력창이 가려지지 않는다. |

- 모든 관련 테스트가 같은 환경에서 재실행 가능하게 통과하고, 수정한 분기에는 실패 전 재현이 있는 회귀 확인을 둔다. 변경 없는 단순 코드와 같은 모양의 테스트는 추가하지 않는다.
- ESLint 출력의 확인된 `set-state-in-effect` 오류가 0건이고, TypeScript 오류가 0건이다. 다른 규칙 오류도 [05-release-PLAN.md](05-release-PLAN.md)의 게이트에 인계한다.
- 브라우저에서 두 번 연속 전송, 진행 중 중지와 재전송, Kimi 거절 뒤 전환, 승인·질문 답변/만료, 보관·복원 후 새로고침을 순서대로 수행한다. 실패 응답과 끊긴 소켓에서는 초안·대기 상태가 거짓 성공으로 보이지 않아야 한다.
- 긴 대화에서 한국어 조합 중 Enter가 전송되지 않고 조합 후 Enter가 한 번만 전송된다. 입력 중 현저한 지연이 없고, 하단에서만 자동 스크롤되며 과거 메시지를 읽을 때 화면이 강제로 내려가지 않는다. 화면 크기를 바꿔도 입력창과 마지막 메시지에 접근할 수 있다.
- 보관·복원 뒤 동일한 workspace ID, 폴더, 기록을 읽을 수 있다. 테스트 중 만든 임시 데이터로 확인하고 기존 사용자 데이터를 지우지 않는다.

## 다른 계획과 의존 관계

- [01-environment-PLAN.md](01-environment-PLAN.md) → 이 단계: 재현 가능한 DB fixture와 웹 도구가 있어야 실패 원인을 분류할 수 있다.
- [03-design-PLAN.md](03-design-PLAN.md)는 환경·안정화와 병행해 시안을 만들 수 있다. 대화·승인·질문·보관 상태의 의미는 이 단계의 검증 결과를 받아야 한다.
- [04-ui-independence-PLAN.md](04-ui-independence-PLAN.md)는 디자인 결정과 안정적인 핵심 동작 계약 이후 해당 화면을 교체한다. 컴포넌트 변경이 스크롤·키보드 흐름을 다시 깨면 이 단계의 시나리오로 확인한다.
- [05-release-PLAN.md](05-release-PLAN.md)의 CI 골격은 일찍 시작할 수 있다. alpha.1 릴리스 게이트는 01·02·04의 검증 결과 뒤에 닫는다.

## 위험, 되돌리기, 미결정

- Kimi CLI 이벤트 형식과 계정별 거절 문구가 바뀌면 fake CLI 테스트만으로 실제 동작을 보장할 수 없다. 민감 정보를 남기지 않는 실사용 재현 로그로 이벤트 모양을 확인하고, 환경이 없으면 미검증으로 표시한다.
- 중지·재연결 로직의 회귀는 실행 중인 프로세스나 승인 대기를 고아 상태로 만들 수 있다. 변경 단위를 서버·UI 흐름별로 작게 유지하고, 실패하면 해당 변경만 되돌린 뒤 runner 종료·재접속 회귀를 다시 확인한다.
- 보관은 현재 DB의 표시 플래그다. UI 변경이 기록 삭제나 새 workspace 생성으로 이어지면 배포하지 않고 기존 PATCH/목록 동작으로 되돌린다. 데이터베이스 롤백을 가정하지 않는다.
- `message-scroller`는 현재 `@shadcn/react`에 의존한다. 04에서 대체할 때 입력·스크롤 동작을 같은 시나리오로 다시 검사한다.
- 사용자에게만 확인할 결정: 현재 없음. 기존 로드맵과 구현이 보관 데이터 유지, 명시적 중지, 데몬 호스팅 단일 UI의 기준을 이미 정한다. 그 기준을 바꿔야 하는 요구가 실제로 나오면 그때 선택지를 제시한다.

**현재 남은 작업과 검증:** 서버 수신 확인 및 재전송 규칙, 답변 무출력 타임아웃, 실제 브라우저·IME와 계정이 필요한 수동 시나리오. 실패 기준선과 자동 검증 결과는 이 문서와 [02-baseline-RESULTS.md](02-baseline-RESULTS.md)에 기록한다.
