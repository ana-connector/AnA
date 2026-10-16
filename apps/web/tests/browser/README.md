# 브라우저 회귀 검사

`@shadcn/react`에서 가져오던 메시지 스크롤러와 질문 폼을 ANA가 직접 구현하면서,
그 동작을 실제 브라우저에서 확인하기 위해 둔 검사다. 린트·타입 검사·빌드는 이
동작을 대신하지 못한다. 대상 화면은 개발 모드에서만 열리는
[`/ui-harness`](../../src/app/ui-harness/page.tsx)다.

## 실행

```bash
# 1. 개발 서버 (프로덕션 빌드에서는 하네스가 비어 있으므로 dev 로 띄운다)
cd apps/web && npm run dev -- -p 3210

# 2. 다른 셸에서
python -m playwright install chromium   # 처음 한 번
python apps/web/tests/browser/test_scroller_and_questions.py
python apps/web/tests/browser/test_skip_and_keyboard.py
```

두 스크립트 모두 통과한 항목을 한 줄씩 찍고, 하나라도 실패하면 종료 코드 1을
돌려준다. 포트를 바꿨다면 각 파일 상단의 `URL`을 함께 바꾼다.

## 무엇을 확인하는가

`test_scroller_and_questions.py`

- 대화를 열면 가장 최근 메시지에서 시작하고, 그동안 점프 버튼은 비활성이다.
- 새 메시지와 길어지는 답변을 따라 바닥에 붙어 있는다.
- 위로 올리면 따라가기를 멈추고 점프 버튼이 활성화되며, 그 뒤 새 메시지가 와도
  읽던 위치를 빼앗지 않는다.
- 점프 버튼을 누르면 다시 끝으로 간다.
- 질문은 한 번에 하나만 보이고 진행 표시가 맞으며, 필수 질문은 Next를 막고
  건너뛰기를 제공하지 않는다.
- 숫자 키로 선택, 복수 선택 유지, 이전으로 돌아가도 앞선 답이 남는다.
- 제출이 `(requestId, (string | string[])[])` 형태로 모든 답을 넘기고, 그 뒤
  카드가 대기 상태로 잠긴다.
- 콘솔·페이지 오류 0건(하이드레이션 불일치 포함).

`test_skip_and_keyboard.py`

- 선택 질문을 건너뛰면 그 질문은 제출에서 빠진다(단일은 `""`, 복수는 `[]`).
- Tab만으로 선택지에 닿고 Space로 고를 수 있다.
- Enter로 다음 질문, ArrowLeft로 이전 질문으로 가며, 질문이 바뀌면 포커스도
  따라간다. 숨은 질문은 포커스 순서에서 빠진다.
- 스크롤 영역이 키보드로 스크롤되고, 그때 점프 버튼이 활성화된다.
