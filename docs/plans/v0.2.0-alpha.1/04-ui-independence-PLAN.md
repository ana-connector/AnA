# v0.2.0-alpha.1 / 04 — 독자 UI 적용과 shadcn 실행 의존 제거

상태: **미구현**. 이 문서는 작업 계획이며 코드 변경·테스트 통과·출시를 뜻하지 않는다.

## 목표와 경계

- [제품 로드맵](../v1.0.0-product-roadmap.md)의 v0.2.0-alpha.1 범위에서 공통 UI의 소유권을 AnA로 옮기고 채팅·입력창·사이드바·보관 목록·승인·질문·작업 카드에 03의 디자인 규칙을 단계적으로 적용한다.
- 현재 `@shadcn/react`에 연결된 메시지 스크롤과 질문 폼을 동일한 사용자 동작을 갖는 자체 구현으로 바꾼다. 기존 컴포넌트의 호출 규약은 가능하면 유지해 화면 변경을 작게 나눈다.
- 모든 소비처를 옮긴 뒤에만 shadcn 전용 패키지, CSS import, 생성 설정을 제거한다. 접근성 기반 `@base-ui/react`는 독립 UI의 구현 부품으로 남겨도 된다.
- 이 단계는 데몬이 호스팅할 **하나의 웹 UI**를 정리한다. Windows·Debian·Android별 화면 복제, 네이티브 셸 구현, 실제 단일 출처 전환은 범위 밖이다. 후자는 로드맵상 v0.3 대상이다.
- 서버 API·WebSocket 주소 계산은 [05-release-PLAN.md](05-release-PLAN.md), 기존 연결 동작의 안정화는 [02-stability-PLAN.md](02-stability-PLAN.md)가 담당한다. 이 문서에서 서버 프로토콜이나 DB를 바꾸지 않는다.

## 현재 코드에서 확인한 사실

| 항목 | 읽기 전용 확인 결과 | 구현 시 확인할 점 |
| --- | --- | --- |
| 직접 실행 의존 | [`message-scroller.tsx`](../../../apps/web/src/components/ui/message-scroller.tsx), [`questionnaire.tsx`](../../../apps/web/src/components/ui/questionnaire.tsx)가 각각 `@shadcn/react/message-scroller`, `@shadcn/react/questionnaire`를 import한다. | 두 파일의 import만 지워도 동작이 같아지는 것은 아니다. primitive가 맡던 상태·이벤트·접근성을 대체해야 한다. |
| 호출 위치 | [`chat-page.tsx`](../../../apps/web/src/components/chat-page.tsx)는 스크롤 Provider/Root/Viewport/Content/Item/Button을 사용한다. [`question-card.tsx`](../../../apps/web/src/components/ui/question-card.tsx)는 질문 단계·선택·입력·오류·이전/건너뛰기/다음/제출을 사용한다. | 새 구현의 props와 콜백 전달이 실제 두 호출 위치에 맞아야 한다. |
| 제거 대상 | [`package.json`](../../../apps/web/package.json)에 `@shadcn/react`, `shadcn`, `@base-ui/react`가 있다. [`globals.css`](../../../apps/web/src/app/globals.css)는 `shadcn/tailwind.css`를 import한다. [`components.json`](../../../apps/web/components.json)은 생성 설정이다. lockfile은 [`package-lock.json`](../../../apps/web/package-lock.json)이다. | CSS import가 제공하던 유틸리티·테마 효과를 확인하고 필요한 것만 자체 CSS로 옮긴다. lockfile도 패키지 변경과 함께 갱신한다. |
| 기존 공통 UI | [`button.tsx`](../../../apps/web/src/components/ui/button.tsx), [`input.tsx`](../../../apps/web/src/components/ui/input.tsx), [`dialog.tsx`](../../../apps/web/src/components/ui/dialog.tsx), [`sidebar.tsx`](../../../apps/web/src/components/ui/sidebar.tsx), [`scroll-area.tsx`](../../../apps/web/src/components/ui/scroll-area.tsx)는 Base UI를 활용한다. | 모든 `ui/` 파일을 한꺼번에 재작성하거나 Base UI를 제거할 필요는 없다. |
| 질문 제출 | [`question-card.tsx`](../../../apps/web/src/components/ui/question-card.tsx)는 `FormData`에서 각 `q{index}`의 값을 읽고 단일 답은 문자열, 복수 답은 배열로 만들어 `onAnswer(request_id, answers)`를 호출한다. 성공 시 중복 제출을 막는다. | 타입·순서·선택 해제/재선택·빈 답의 의미를 보존한다. |
| 스크롤 | [`chat-page.tsx`](../../../apps/web/src/components/chat-page.tsx)는 `autoScroll`을 쓰며 일반 메시지에는 `scrollAnchor`를 주지 않는다. [`message-scroller.tsx`](../../../apps/web/src/components/ui/message-scroller.tsx)는 과거 `content-visibility`가 긴 답변의 위치 계산을 틀리게 했다는 주석을 가진다. | 스트리밍 중 바닥 추적, 위로 읽을 때 위치 유지, 새 항목·긴 답변·점프 버튼을 관찰해 계약으로 고정한다. |

위 표는 소스와 선언 파일에서 확인한 사실이다. 패키지가 만든 CSS의 실제 영향 범위, 현재 브라우저별 키보드 동작, 기존 라이선스 고지의 위치는 **아직 검증하지 않았다**. 구현 전에 비교·기록한다.

## 이 단계가 지켜야 할 공통 UI 계약

1. 버튼·입력·메뉴·다이얼로그·카드는 03에서 정한 색·타이포그래피·간격·포커스·상태 규칙을 따른다. `data-slot`과 공통 컴포넌트의 공개 props는 소비처를 옮기는 동안 유지하고, 변경이 불가피하면 사용처를 함께 갱신한다.
2. 스크롤은 처음 열 때 최신 내용에 도달할 수 있고, 바닥을 보고 있을 때 새 스트림을 따라간다. 사용자가 위쪽을 읽는 동안 자동 이동으로 읽던 위치를 빼앗지 않는다. 최신 항목 버튼은 키보드로 누를 수 있고 활성/비활성 상태를 알 수 있어야 한다.
3. 질문은 단일 선택과 복수 선택, 자유 입력, 필수·선택 질문, 여러 단계, 이전/다음/건너뛰기, 오류 표시, 숫자 단축키의 현재 의미를 유지한다. 라벨·설명·오류를 입력과 연결하고 현재 단계 및 포커스 이동을 확인한다.
4. `QuestionCard`의 제출 계약인 `(requestId, (string | string[])[]) => boolean`과 성공 후 중복 제출 방지를 유지한다. 승인 카드의 승인/거부/만료 표시와 서버 응답 계약은 유지한다.
5. 채팅·보관/복원·사이드바 전환·입력창 초안·작업 카드의 상태는 시각 변경 중에도 보존한다. 원격 접속과 모바일 화면은 같은 데몬 UI의 반응형 배치로 취급한다.
6. 가져온 코드나 CSS를 유지·수정할 때 기존 저작권·라이선스 고지를 확인하고 보존한다. 새 구현이 참고한 외부 코드의 고지 필요 여부도 기록한다.

## 순서가 있는 작은 작업

### 1. 동작·의존 목록 고정

- 입력: 현재 소스, [01-environment-PLAN.md](01-environment-PLAN.md)의 재현 가능한 웹 환경, [02-stability-PLAN.md](02-stability-PLAN.md)의 핵심 흐름 기준.
- 작업: 두 primitive의 공개 props·상태·키보드·폼 동작과 CSS 기여를 브라우저에서 기록한다. `rg`로 직접 import, CSS, 구성 파일, 고지 목록을 재확인한다.
- 산출물: 스크롤/질문 동작 점검표와 실제 의존 목록. 확인하지 못한 항목은 미확인으로 표시한다.

### 2. 공통 컴포넌트 계약 정리

- 입력: 1의 동작 점검표, [03-design-PLAN.md](03-design-PLAN.md)의 확정된 시안과 토큰.
- 작업: 공통 버튼·입력·메뉴·다이얼로그·카드의 쓰임과 키보드/포커스 계약을 정리한다. 필요한 최소한의 공통 컴포넌트만 조정한다.
- 산출물: 화면 소비자가 가져다 쓸 UI 계약과 시안 대비 확인 기록.

### 3. 메시지 스크롤 교체

- 입력: 1의 스크롤 계약, 02로 안정화된 채팅 동작.
- 작업: 메시지 스크롤 primitive를 자체 상태·DOM 처리로 교체한다. 자동 추적, 수동 위치 유지, 항목 추가/크기 변화, 점프 버튼, 스크롤 영역 접근성을 구현한다.
- 산출물: `message-scroller.tsx`의 직접 `@shadcn/react` import 제거와 채팅 수동 회귀 기록.

### 4. 질문 단계 교체

- 입력: 1의 질문 계약과 기존 `QuestionCard` 제출 형식.
- 작업: 질문 단계를 자체 상태/폼 또는 기존 플랫폼·Base UI 부품으로 구현한다. 기존 컴포넌트 경계가 도움 되면 유지하고, 불필요한 래퍼는 줄인다.
- 산출물: `questionnaire.tsx`의 직접 import 제거, 질문 응답·검증·접근성 확인 기록.

### 5. 핵심 화면별 적용

- 입력: 2~4 완료, 03 디자인 방향.
- 작업: 채팅/입력창 → 사이드바·보관 목록 → 승인·질문·작업 카드 순으로 적용한다. 화면별 변경 후 바로 기능·키보드·좁은 화면을 비교한다.
- 산출물: 각 화면의 전/후 상태와 통과/실패 목록. 미완료 화면은 이 알파 완료로 표시하지 않는다.

### 6. shadcn 전용 항목 제거

- 입력: 모든 런타임 소비처 이전과 CSS 대체 확인.
- 작업: `@shadcn/react`, `shadcn`, `shadcn/tailwind.css`, `components.json`을 제거하고 lockfile을 갱신한다. 제거 뒤 전체 import·생성 설정·필요한 CSS의 잔여 참조를 찾는다.
- 산출물: shadcn 전용 실행/생성 의존 0건, 빌드 결과와 라이선스 고지 보존 기록.

3과 4는 각각 독립된 작은 변경으로 진행할 수 있다. 5의 화면별 적용은 03의 방향과 02의 안정된 동작 기준을 받은 뒤 시작한다. 6은 `@shadcn/react`를 쓰는 소비자가 완전히 없어졌음을 확인한 뒤에만 한다.

## 검증과 수용 기준

- 정적 확인: `cd apps/web && npx tsc --noEmit`, `npm run lint`, `npm run build`. 실행 환경이 준비된 뒤 결과를 기록한다. 현재 통과했다고 주장하지 않는다. 기존 린트 실패가 있다면 변경 전/후를 비교해 새 오류와 구분한다.
- 의존 확인: `rg -n '@shadcn/react|shadcn/tailwind.css|"shadcn"|ui.shadcn.com' apps/web/src apps/web/package.json apps/web/package-lock.json apps/web/components.json`을 제거 전후에 사용한다. 최종에는 제거된 `components.json` 경로를 빼고 실행하여 빈 결과를 확인한다. `@base-ui/react` 사용은 실패가 아니다.
- 스크롤 수동 회귀: 새 대화, 기록 불러오기, 긴 한국어/코드 답변, 빠른 스트리밍, 위로 읽는 중 도착한 새 내용, 최신 항목 버튼, 창 크기 변경을 검증한다. 읽던 위치를 잃거나 마지막 메시지가 가려지면 실패다.
- 질문 수동 회귀: 단일/복수 선택, 자유 입력, 필수 항목 누락, 선택 항목 건너뛰기, 여러 단계의 이전/다음, 숫자 단축키, 오류 수정, 성공/실패 콜백, 연속 클릭을 검증한다. 답 배열의 순서·타입 또는 제출 횟수가 바뀌면 실패다.
- 핵심 화면 회귀: 채팅 전송·중지, 사이드바 전환, 보관/복원, 승인 응답, 작업 카드 표시, 입력창 초안 보존을 데스크톱과 좁은 뷰포트에서 점검한다. 키보드만으로 동작하고 포커스가 보이며 오류·비활성 상태를 구별할 수 있어야 한다.
- 자동화는 가장 깨지기 쉬운 스크롤 위치와 질문 응답 형식 같은 사용자 계약에만 추가한다. 결과가 불안정한 DOM 세부사항을 그대로 복제하는 테스트는 만들지 않는다.

## 의존 관계와 위험 관리

- [01-environment-PLAN.md](01-environment-PLAN.md)는 웹 빌드·수동 검증 환경을 마련하고, [02-stability-PLAN.md](02-stability-PLAN.md)는 회귀 판단에 쓸 채팅·승인·질문 동작을 안정화한다.
- [03-design-PLAN.md](03-design-PLAN.md)는 UI 토큰과 핵심 화면의 방향을 결정한다. 이 문서의 래퍼 대체를 위한 동작 조사·구현 설계는 먼저 진행할 수 있으나 전체 화면 채택은 03의 결정을 따른다.
- [05-release-PLAN.md](05-release-PLAN.md)는 검증 기반 시설을 앞서 준비할 수 있다. alpha.1 출시 게이트는 01·02·04의 실제 통과 기록을 받은 뒤에만 판단한다.
- 위험: 스크롤의 비동기 높이 계산, 질문 폼의 암묵적 검증/단축키, shadcn CSS 제거로 인한 숨은 스타일 차이, 수정한 외부 코드의 고지 누락. 변경마다 이전 화면과 동작을 기록하고 문제가 생긴 범위만 되돌린다. 패키지 제거는 마지막 별도 변경으로 남겨 기능 변경을 역추적할 수 있게 한다.
- 롤백은 해당 화면의 마지막 동작 확인 버전으로 되돌리고, 질문 제출 데이터와 서버 저장 형식을 바꾸지 않아 기존 데이터를 유지한다. 회귀가 남으면 shadcn 제거를 완료 처리하지 않는다.

## 사용자에게만 남길 결정

- 03의 시안으로도 정할 수 없는 브랜드 방향이나 외부 자산 라이선스/비용 선택이 생긴 경우에만 사용자에게 실제 선택지를 제시한다. 컴포넌트 내부 상태 관리 방식, Base UI 유지, 마이그레이션 순서는 구현자가 결정한다.
- 이 문서 작성 시점에는 진행을 막는 사용자 결정을 확인하지 못했다. 셸 프레임워크·앱 배포 방식은 이번 단계의 결정 사항이 아니다.

**바로 시작할 작업:** 재현 가능한 환경에서 `message-scroller.tsx`와 `questionnaire.tsx`의 현재 동작·키보드·CSS 영향 점검표를 만들고, 각 호출 위치의 props/답 형식을 같은 표에 연결한다.
