# 서버 테스트 실패 기준선

## 실행 조건과 명령

작업 디렉터리는 `/home/hello/ana`였다. 테스트 실행 명령에서 `PGHOSTADDR`와 `DATABASE_URL`을 별도로 지정하지 않았다. 전체 suite를 단일 pytest 프로세스로 실행하면 약 87% 지점에서 OOM 종료된 이전 결과가 있어, 파일마다 독립 프로세스를 사용했다. 다시 읽은 `/sys/fs/cgroup/memory.events`는 `oom_kill=1`, `memory.peak`는 6,291,202,048바이트, `memory.max`는 `max`다. 따라서 **설정된 cgroup 메모리 한도가 6.3 GB였다는 뜻은 아니다**. 피크는 같은 cgroup의 다른 프로세스도 포함할 수 있다. 표의 시간은 벽시계 시간이 아니라 pytest가 각 파일의 마지막 줄에 보고한 시간이다.

파일별 실행 명령(각 파일의 전체 출력은 `/tmp/ana_baseline_final/`에, 파일별 한 줄 요약은 `/tmp/ana_baseline_final.log`에 저장):

```bash
mkdir -p /tmp/ana_baseline_final
: > /tmp/ana_baseline_final.log
find apps/server/tests -maxdepth 1 -type f -name 'test_*.py' -print0 | sort -z | while IFS= read -r -d '' file; do
  out="/tmp/ana_baseline_final/$(basename "$file").log"
  start=$SECONDS
  timeout 420 .venv/bin/python -m pytest "$file" -q -rfE -p no:cacheprovider > "$out" 2>&1
  status=$?
  elapsed=$((SECONDS-start))
  summary=$(grep -E '^=+ .* (passed|failed|error|skipped|deselected).* in [0-9.]+s =+$|^=+ no tests ran in [0-9.]+s =+$' "$out" | tail -n 1)
  if [ -z "$summary" ]; then summary=$(tail -n 1 "$out"); fi
  printf '%s | exit=%s | wall=%ss | %s\n' "$file" "$status" "$elapsed" "$summary" | tee -a /tmp/ana_baseline_final.log
done
```

실패한 개별 테스트 재실행 명령(각 테스트에 `--tb=short` 적용):

```bash
mkdir -p /tmp/ana_baseline_reruns
: > /tmp/ana_baseline_reruns.log
n=0
grep -hE '^FAILED apps/server/tests/' /tmp/ana_baseline_final/*.log | sed -E 's/^FAILED (apps\/server\/tests\/[^ ]+).*/\1/' | while IFS= read -r test_id; do
  n=$((n+1))
  out="/tmp/ana_baseline_reruns/$(printf '%02d' "$n").log"
  timeout 420 .venv/bin/python -m pytest "$test_id" -q -rfE -p no:cacheprovider --tb=short > "$out" 2>&1
  status=$?
  printf '%02d | exit=%s | %s\n' "$n" "$status" "$test_id" | tee -a /tmp/ana_baseline_reruns.log
done
```

정리 및 잔여 컨테이너 확인 명령:

```bash
ids=$(docker ps -aq --filter 'name=ana-test-pg')
if [ -n "$ids" ]; then docker rm -f $ids; fi
docker ps -aq --filter 'name=ana-test-pg'
```

`docker ps` 확인 결과 남은 `ana-test-pg` 컨테이너는 없었다.

## 파일별 결과

| test file (`apps/server/tests/`) | passed | failed | errors | duration (s) |
|---|---:|---:|---:|---:|
| `test_ask_user.py` | 22 | 0 | 0 | 3.51 |
| `test_base.py` | 3 | 0 | 0 | 0.02 |
| `test_breaker.py` | 29 | 0 | 0 | 0.06 |
| `test_cancel.py` | 6 | 0 | 0 | 6.96 |
| `test_capabilities_endpoint.py` | 4 | 0 | 0 | 4.00 |
| `test_claude_code_adapter.py` | 13 | 0 | 0 | 0.17 |
| `test_codex_adapter.py` | 16 | 0 | 0 | 0.23 |
| `test_codex_usage.py` | 11 | 0 | 0 | 0.04 |
| `test_containers.py` | 14 | 0 | 0 | 0.07 |
| `test_cooldown.py` | 2 | 0 | 0 | 0.02 |
| `test_delegate_mcp.py` | 3 | 0 | 0 | 0.02 |
| `test_delegation.py` | 9 | 0 | 0 | 1.06 |
| `test_env_builder.py` | 12 | 0 | 0 | 0.04 |
| `test_failover.py` | 18 | 0 | 0 | 7.53 |
| `test_gate_endpoint.py` | 25 | 0 | 0 | 17.30 |
| `test_gate_registry.py` | 7 | 0 | 0 | 0.44 |
| `test_gate_scope.py` | 9 | 0 | 0 | 5.06 |
| `test_governance_audit.py` | 7 | 0 | 0 | 1.73 |
| `test_governance_hook.py` | 8 | 0 | 0 | 3.01 |
| `test_governance_rules.py` | 61 | 0 | 0 | 0.20 |
| `test_governance_wiring.py` | 23 | 2 | 0 | 10.41 |
| `test_graph.py` | 7 | 2 | 0 | 4.29 |
| `test_graph_agents.py` | 6 | 0 | 0 | 3.31 |
| `test_handoff.py` | 6 | 0 | 0 | 0.03 |
| `test_history_endpoint.py` | 12 | 0 | 0 | 5.55 |
| `test_kimi_adapter.py` | 17 | 0 | 0 | 4.97 |
| `test_main.py` | 2 | 0 | 0 | 2.82 |
| `test_mcp_gate.py` | 3 | 1 | 0 | 3.48 |
| `test_mcp_registry.py` | 12 | 0 | 0 | 0.07 |
| `test_mcp_scope.py` | 13 | 0 | 0 | 0.03 |
| `test_mcp_wiring.py` | 13 | 0 | 0 | 0.07 |
| `test_memory_db.py` | 3 | 0 | 0 | 1.55 |
| `test_memory_embeddings.py` | 4 | 0 | 0 | 0.89 |
| `test_memory_rag.py` | 4 | 0 | 0 | 2.29 |
| `test_multi_agent_graph.py` | 29 | 0 | 0 | 14.30 |
| `test_observability.py` | 10 | 0 | 0 | 3.73 |
| `test_planner.py` | 20 | 0 | 0 | 0.05 |
| `test_remote_discord.py` | 16 | 0 | 0 | 1.02 |
| `test_remote_gateway.py` | 8 | 0 | 0 | 0.03 |
| `test_remote_notifier.py` | 9 | 0 | 0 | 0.28 |
| `test_remote_render.py` | 25 | 0 | 0 | 0.05 |
| `test_routing_and_limits.py` | 44 | 0 | 0 | 6.94 |
| `test_runners.py` | 13 | 0 | 0 | 0.13 |
| `test_schedules.py` | 36 | 0 | 0 | 2.68 |
| `test_secrets.py` | 24 | 0 | 0 | 3.86 |
| `test_shared_context.py` | 14 | 0 | 0 | 0.09 |
| `test_skills.py` | 15 | 0 | 0 | 0.16 |
| `test_subagent_scope.py` | 35 | 0 | 0 | 0.07 |
| `test_supervisor.py` | 13 | 0 | 0 | 1.81 |
| `test_task_graph.py` | 11 | 0 | 0 | 0.35 |
| `test_transcript.py` | 2 | 0 | 0 | 0.02 |
| `test_turn_timeline.py` | 1 | 0 | 0 | 0.02 |
| `test_usage_reporting.py` | 7 | 0 | 0 | 2.83 |
| `test_usage_sync.py` | 12 | 0 | 0 | 4.64 |
| `test_verification.py` | 14 | 1 | 0 | 1.26 |
| `test_websocket.py` | 8 | 3 | 0 | 10.39 |
| `test_websocket_agents.py` | 6 | 0 | 0 | 4.81 |
| `test_workspace_isolation.py` | 23 | 0 | 0 | 7.48 |
| `test_workspaces.py` | 33 | 3 | 0 | 6.05 |
| `test_worktrees.py` | 13 | 0 | 0 | 2.29 |
| `test_yolo_mode.py` | 15 | 0 | 0 | 7.67 |

**총계: 수집 872 / 통과 860 / 실패 12 / 오류 0.** 실패 원인 분류는 제품 결함 1건, 오래된 테스트 기대값 6건, 환경/인프라 5건이다.

## 수정 후 재검증 (2026-10-12)

위 표는 **수정 전 기준선**이다. 현재 main 작업 트리의 서버·테스트 수정 후 `apps/server/tests/test_*.py` 61개 파일을 파일당 독립 pytest 프로세스로 순차 실행했다. 각 명령은 `.venv/bin/python -m pytest "$file" -q -rfE -p no:cacheprovider`였고, 420초 제한과 모든 종료 코드 집계를 적용했다. 원출력은 `/tmp/ana-plan02-integrated-suite/`, 요약은 `/tmp/ana-plan02-integrated-suite.log`에 있다. 그 실행 중 runner 버퍼와 질문 취소 감사 기록을 최종 수정했으므로 영향받을 수 있는 앞쪽 7개 파일을 최종 코드에서 다시 실행했다. 재실행 출력은 `/tmp/ana-final-current-test_*.py.log`, 합친 파일별 요약은 `/tmp/ana-plan02-final-summary.log`에 있다.

**최종 파일별 집계: 61개 파일 / 889개 통과 / 실패 0 / 오류 0 / 건너뜀 0.** 첫 기준선보다 테스트 17개가 늘었다. Kimi adapter 17개, failover 20개, cancel 6개, runner 18개, WebSocket 11개, 작업공간 40개, 원격 알림 10개가 포함된다. 이 수치는 실제 브라우저·IME, 실제 Kimi 계정, CI 실행의 통과를 뜻하지 않는다. 단일 프로세스 전체 실행은 OOM 이력이 있어 반복하지 않았고, 개발·운영 DB/볼륨은 사용하지 않았다.

### 후속 릴레이 수정 재검증

위 889개 집계 뒤에 예외가 난 턴 때문에 대기 prompt가 멈추는 경로를 수정하고 `test_runners.py`에 회귀 검사 1개를 추가했다. 변경된 파일의 전체 검사를 `.venv/bin/python -m pytest -q apps/server/tests/test_runners.py -p no:cacheprovider`로 다시 실행해 **19개 통과 / 실패 0**이었다. 나머지 60개 파일은 코드 영향이 없으므로 위 파일별 결과를 재사용한다. 이를 합친 최신 파일별 집계는 **61개 파일 / 890개 통과 / 실패 0 / 오류 0 / 건너뜀 0**이다. 같은 작업 트리에서 `apps/web`의 `npm run lint`도 다시 통과했다. 이 후속 수정 뒤 단일 프로세스 전체 실행이나 실제 브라우저 검사는 하지 않았다.

## 실패별 진단

아래 traceback은 각 단일 테스트를 `--tb=short`로 다시 실행한 결과에서 핵심 3~5줄을 발췌했다. 긴 객체 출력에 들어 있는 토큰 값은 보고서에서 생략했다.

### `apps/server/tests/test_governance_wiring.py::test_build_adapter_leaves_the_gate_off_when_disabled`

- 단언 결과: `ANA_GATE_TOKEN`이 `adapter.env`에 없어야 하지만 존재했다.
- 분류: **제품 결함**. 부모 프로세스에 이미 `ANA_GATE_TOKEN`이 설정되어 있다. `build_adapter()`는 `ANA_GOVERNANCE=off`일 때 새 토큰을 넣지 않지만 상속한 토큰도 제거하지 않아, 비활성화된 CLI 환경에 게이트 토큰이 남는다.

```text
apps/server/tests/test_governance_wiring.py:187: in test_build_adapter_leaves_the_gate_off_when_disabled
    assert "ANA_GATE_TOKEN" not in adapter.env
E   AssertionError: assert 'ANA_GATE_TOKEN' not in adapter.env
```

### `apps/server/tests/test_governance_wiring.py::test_background_turns_off_releases_the_token_with_the_connection`

- 단언 결과: 5초 후 빈 세션 목록을 기대했지만 `GateSession` 한 개가 남았다.
- 분류: **환경/인프라**. `ANA_BACKGROUND_TURNS=off`이면 서버의 WebSocket `finally`에서 runner를 제거하도록 구현되어 있다. 이 테스트의 Starlette `TestClient`는 context 종료 때 disconnect를 큐에 넣고 ASGI 작업을 곧바로 취소하므로, 서버의 종료 경로가 실행되기 전에 테스트 세션이 남을 수 있다; 실제 클라이언트의 종료 동작까지 이 결과만으로 단정할 수 없다.

```text
apps/server/tests/test_governance_wiring.py:276: in test_background_turns_off_releases_the_token_with_the_connection
    assert _session_for(registry, "gate-ws-off") == []
E   AssertionError: assert [GateSession(...gate-ws-off')] == []
E     Left contains one more item: GateSession(...)
```

### `apps/server/tests/test_graph.py::test_error_only_turn_still_reaches_transcript_and_does_not_poison_session_id`

- 단언 결과: 원문 `claude: command not found`를 기대했지만 `요청을 완료하지 못했습니다. 서버 오류 기록을 확인해 주세요.`가 기록되었다.
- 분류: **오래된 테스트 기대값**. `graph.py`는 실패한 CLI의 원문 오류 대신 공통 오류 문구를 transcript에 기록하도록 현재 구현되어 있다. transcript 기록 자체는 호출되었고, 실패 지점은 응답 문자열의 기대값이다.

```text
apps/server/tests/test_graph.py:242: in test_error_only_turn_still_reaches_transcript_and_does_not_poison_session_id
    assert kwargs["response"] == "claude: command not found"
E   AssertionError: assert '요청을 완료하지 못했습... 기록을 확인해 주세요.' == 'claude: command not found'
E     + 요청을 완료하지 못했습니다. 서버 오류 기록을 확인해 주세요.
```

### `apps/server/tests/test_graph.py::test_mid_turn_exception_still_reaches_transcript_before_propagating`

- 단언 결과: transcript 응답에 `boom`이 있어야 하지만 공통 서버 오류 문구가 기록되었다.
- 분류: **오래된 테스트 기대값**. `graph.py`의 예외 경로도 transcript에 원문 예외 대신 공통 오류 문구를 저장한다. `transcript.append` 호출은 확인되므로 실패는 기록 누락이 아니라 문자열 기대값 차이다.

```text
apps/server/tests/test_graph.py:299: in test_mid_turn_exception_still_reaches_transcript_before_propagating
    assert "boom" in kwargs["response"]
E   AssertionError: assert 'boom' in '요청을 완료하지 못했습니다. 서버 오류 기록을 확인해 주세요.'
```

### `apps/server/tests/test_mcp_gate.py::test_lead_still_gets_no_tools`

- 단언 결과: `deny`를 기대했지만 `allow`가 반환되었다.
- 분류: **오래된 테스트 기대값**. 현재 gate 구현은 일반 lead turn에 작업 도구를 허용하고, 명시적 `lead_scope`가 있을 때 그 범위를 적용한다. 테스트는 이전의 lead 전면 차단 정책을 기대한다.

```text
apps/server/tests/test_mcp_gate.py:100: in test_lead_still_gets_no_tools
    assert response.json()["decision"] == "deny"
E   AssertionError: assert 'allow' == 'deny'
E     - deny
E     + allow
```

### `apps/server/tests/test_verification.py::test_an_ask_verdict_is_refused_too`

- 단언 결과: `result.ran=False`를 기대했지만 `True`였고 `curl` 명령은 성공했다.
- 분류: **오래된 테스트 기대값**. `config/governance.yaml`의 현재 `network-egress` 규칙은 자료 조회용 `curl`을 `allow`한다. `verify()`는 허용 판정을 그대로 실행하므로, `ask` 판정을 기대하는 테스트 설명이 현 규칙과 맞지 않는다.

```text
apps/server/tests/test_verification.py:48: in test_an_ask_verdict_is_refused_too
    assert not result.ran
E   AssertionError: assert not True
E    +  where True = VerificationResult(ran=True, passed=True, command='curl https://example.com', ...)
```

### `apps/server/tests/test_websocket.py::test_websocket_allows_connection_with_allowed_origin`

- 단언 결과: JSON 프레임을 기대했지만 WebSocket이 코드 1008로 닫혔다.
- 분류: **환경/인프라**. 테스트는 `http://localhost:3000`을 보냈지만 현재 프로세스의 `ANA_ALLOWED_ORIGINS`에는 3005 포트 주소만 있다. 서버는 현재 허용 목록에 따라 1008로 연결을 닫았고, 테스트가 환경별 목록을 격리하지 않았다.

```text
apps/server/tests/test_websocket.py:165: in test_websocket_allows_connection_with_allowed_origin
    first = ws.receive_json()
.venv/lib/python3.13/site-packages/starlette/testclient.py:170: in _raise_on_close
    raise WebSocketDisconnect(code=message.get("code", 1000), reason=message.get("reason", ""))
E   starlette.websockets.WebSocketDisconnect
```

### `apps/server/tests/test_websocket.py::test_websocket_relays_error_when_run_stream_raises`

- 단언 결과: 첫 콘텐츠 프레임으로 `error`를 기대했지만 `failover`가 왔다.
- 분류: **오래된 테스트 기대값**. 현재 graph는 첫 adapter 예외가 나면 다음 모델로 넘어가며 `failover` 프레임을 먼저 보낸다. 테스트는 이 복구 경로 이전의 즉시 `error` 프레임을 기대한다.

```text
apps/server/tests/test_websocket.py:199: in test_websocket_relays_error_when_run_stream_raises
    assert event["type"] == "error"
E   AssertionError: assert 'failover' == 'error'
E     - error
E     + failover
```

### `apps/server/tests/test_websocket.py::test_websocket_closes_connection_after_relay_failure`

- 단언 결과: 첫 콘텐츠 프레임으로 `error`를 기대했지만 `failover`가 왔다.
- 분류: **오래된 테스트 기대값**. 같은 예외 복구 경로에서 첫 프레임은 `failover`이다. 이 assertion은 이후 연결 종료까지 검사하기 전에 실패하므로, 이번 결과만으로 연결 종료 동작의 옳고 그름은 알 수 없다.

```text
apps/server/tests/test_websocket.py:217: in test_websocket_closes_connection_after_relay_failure
    assert next_content(ws)["type"] == "error"
E   AssertionError: assert 'failover' == 'error'
E     - error
E     + failover
```

### `apps/server/tests/test_workspaces.py::test_api_create_list_rename_delete`

- 단언 결과: `['api-project']`를 기대했지만 `['ana-self', 'api-project']`였다.
- 분류: **환경/인프라**. 현재 프로세스의 `ANA_SELF_WORKSPACE=/home/hello/ana` 때문에 앱 시작 시 `ana-self`가 등록된다. 테스트는 자체 작업공간이 없는 환경을 전제하고 목록을 단정한다.

```text
apps/server/tests/test_workspaces.py:257: in test_api_create_list_rename_delete
    assert [w["id"] for w in listed] == ["api-project"]
E   AssertionError: assert ['ana-self', 'api-project'] == ['api-project']
E     At index 0 diff: 'ana-self' != 'api-project'
E     Left contains one more item: 'api-project'
```

### `apps/server/tests/test_workspaces.py::test_websocket_still_accepts_thread_id`

- 단언 결과: 첫 작업공간으로 `legacy-one`을 기대했지만 `ana-self`였다.
- 분류: **환경/인프라**. `ANA_SELF_WORKSPACE`가 설정되어 `ana-self`가 이미 첫 번째 목록 항목이다. 실패한 assertion은 WebSocket의 `thread_id` 수락이 아니라 목록의 첫 항목 순서에 대한 환경 의존 기대값이다.

```text
apps/server/tests/test_workspaces.py:297: in test_websocket_still_accepts_thread_id
    assert client.get("/workspaces").json()["workspaces"][0]["id"] == "legacy-one"
E   AssertionError: assert 'ana-self' == 'legacy-one'
E     - legacy-one
E     + ana-self
```

### `apps/server/tests/test_workspaces.py::test_capabilities_report_the_self_workspace`

- 단언 결과: `self_workspace is None`을 기대했지만 값이 `ana-self`였다.
- 분류: **환경/인프라**. `ANA_SELF_WORKSPACE=/home/hello/ana`가 실제로 설정되어 있고 디렉터리도 존재한다. capabilities가 `ana-self`를 보고한 것은 이 환경에 맞으며, 테스트는 변수를 비운 환경을 가정한다.

```text
apps/server/tests/test_workspaces.py:471: in test_capabilities_report_the_self_workspace
    assert payload["self_workspace"] is None
E   AssertionError: assert 'ana-self' is None
```

---

## 최종 재실행 (2026-10-16)

수정이 모두 들어간 뒤 같은 파일별 방식으로 61개 파일을 다시 돌렸다.

```bash
for f in apps/server/tests/test_*.py; do
  timeout 420 /home/hello/ana/.venv/bin/python -m pytest "$f" -q -rfE -p no:cacheprovider
done
```

- **총계: 61개 파일 / 892 통과 / 0 실패 / 0 오류.** 실행 후 `ana-test-pg-*` 컨테이너는 남지 않았다. 이후 중지 뒤 프롬프트 회귀 검사 1개를 `test_runners.py`에 더해 최종 집계는 **893 통과**이며, 출시 게이트의 수치는 [05](05-release-PLAN.md)에 있다.
- 이 실행 직전 두 가지 환경 결손을 고쳐야 했다. 둘 다 제품 결함이 아니며 자세한 내용은 [01-environment-PLAN.md](01-environment-PLAN.md)의 실행 기록에 있다.
  1. `.venv`의 베이스 인터프리터(`/usr/local/bin/python3.13`)가 사라져 모든 파일이 `exit=127`(`timeout: failed to execute process`)로 끝났다. mise의 3.13.14로 다시 연결했다.
  2. `uvx`가 PATH에서 사라져 `config/mcp.yaml`의 `git`·`postgres` 서버가 조용히 비활성화되고 `test_capabilities_endpoint.py::test_the_lead_has_routed_delegation_but_github_stays_worker_only`가 실패했다. `uv` 설치 후 통과한다.
- 웹: `eslint src`, `tsc --noEmit`, `npm run build` 모두 통과(오류 0).
