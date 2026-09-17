# ANA — AI 오케스트레이터 구현 로드맵

## 개요

기획서(7계층 참조 아키텍처, 인지/협업 패턴, 거버넌스 프레임워크)와 사례 분석 보고서를 기반으로 실제 구현 순서를 정리한 문서. 전체 스택(웹 UI + CLI 기반 에이전트 오케스트레이션 + 메모리 + 샌드박스 + 거버넌스 + 관측 + MCP)을 한 번에 구현하는 대신, 독립적인 서브프로젝트 6개로 분해하여 순서대로 진행한다.

핵심 엔진은 Claude Code CLI / Codex CLI를 서브프로세스로 구동하는 방식이며, REST API 호출이 아닌 프로세스 래핑(Process Wrapping) + 실시간 스트리밍 구조를 취한다.

## 확정된 설계 결정 (Core Loop 브레인스토밍 결과)

- **배포 대상**: 단일 사용자, 로컬/내부망 전용으로 시작. 멀티유저 확장은 향후 과제.
- **바인딩 주소**: `0.0.0.0`. 이 서버는 헤드리스 서버이며 사용자는 내부망의 다른 기기(브라우저)에서 접속한다. 단, 외부망(인터넷)에는 노출하지 않고 내부망 신뢰 범위 안에서만 접근 가능하다는 전제. 인증 계층이 없는 상태이므로 이 전제가 깨지면(포트 포워딩, 외부망 노출 등) 즉시 위험해진다는 점을 명시.
- **인증**: 내부망을 신뢰 기반으로 간주하고 인증 계층 없이 시작. 인증 토큰 도입은 우선순위 최하위로 미루며, 필요 없다고 판단되면 아예 구현하지 않을 수도 있다.
- **CLI 인증 모드**: `~/.bashrc`에 정의된 두 모드를 그대로 활용.
  - `use-claude-pro`: 구독 모드, 환경 변수 없이 로컬 로그인 세션 사용.
  - `use-claude-api`: API 빌링 모드, `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`(`panel.tapie.kr/api/claude-code` 경유)을 주입.
  - `env_builder` 모듈이 세션 시작 시 둘 중 하나를 선택해 서브프로세스 환경에 주입. 기본값은 `.env` 파일에 지정. `.env`는 1번 단계에서 파일 기반으로 최소 구현. **웹 UI에서 세션별로 override(간단한 토글)하는 기능은 1번 단계 구현 계획에 태스크로 반영되지 못해 실제로 만들어지지 않았다 — Core Loop 최종 리뷰에서 발견됨.** 이 기능은 별도로 만들지 않고, 프로젝트별/글로벌 범위를 가진 정식 시크릿 관리 UI(3번 단계, Governance)에 합쳐서 그때 함께 구현한다. 그 전까지는 `.env` 수정 + 서버 재시작으로 모드를 바꾼다.
- **작업 디렉토리**: 고정된 테스트용 워크스페이스 폴더 `/home/hello/workspace` 하나로 시작(신규 생성, ANA 프로젝트 자체와 분리). 세션별 격리 워크스페이스는 3번 단계(Sandbox & Governance)에서 구축.
- **권한 승인**: CLI 자체 자동 승인 플래그(`--dangerously-skip-permissions` 류)로 시작해 기본 흐름부터 완성. Human-in-the-loop 승인은 3번 단계에서 웹 UI로 대체.
- **에이전트 어댑터**: 확장 가능한 추상 인터페이스(`run_stream()`, `terminate()`)로 설계. Core Loop 단계에서는 Claude Code CLI 어댑터만 구현하고, Codex CLI 어댑터는 5번 단계(Multi-Agent Orchestration)에서 같은 인터페이스에 얹어 추가. 어댑터는 `model`/`effort`를 생성자 파라미터로 받아 CLI의 `--model`/`--effort` 플래그로 그대로 전달한다(기본값은 `.env`의 `CLAUDE_MODEL`/`CLAUDE_EFFORT`) — 하드코딩하지 않는 이유는 2번 단계(Memory & State)의 failover 로직이 코드 변경 없이 다른 모델/effort로 바로 재시도할 수 있어야 하기 때문.
- **⚠️ 미검증 사항**: `use-claude-api`(API 빌링 모드, `panel.tapie.kr` 경유)가 현재 실제로 동작하는지 검증되지 않았다. Core Loop 구현 시 반드시 실제 호출로 확인하고, 안 되면 `pro` 모드로 우선 진행한다(구현 계획 문서의 Task 10 참고).
- **CLI 프로세스 모델**: "대장 Agent"는 턴마다 새 프로세스를 띄우되 Claude Code CLI의 `--resume`/`--continue` 옵션으로 대화 맥락을 이어받는다(OS 프로세스를 계속 살려두는 방식은 채택하지 않음 — 관리 단순성 우선). 대장 Agent가 세부 작업을 위해 새로운 sub-agent 세션(별도 CLI 프로세스)을 동적으로 파생시키는 기능은 1번 단계 범위에서 제외하고 5번 단계(Multi-Agent Orchestration)에서 구현한다. 1번 단계는 대장 Agent 단독 동작까지만 완성한다.
- **대화 기록(transcript) 보관**: 대장 Agent가 토큰/사용량 한도로 죽을 경우 다른 AI 모델이 이어받을 수 있어야 하므로, 1번 단계에서부터 턴마다 사용자 프롬프트 + 에이전트 응답을 워크스페이스에 간단한 로그 파일(JSON/텍스트)로 누적 저장한다. 단, "한도 감지 시 다른 모델로 자동 전환"하는 failover 로직 자체는 DB가 생기는 2번 단계(Memory & State)에서 구현한다.

## 서브프로젝트 로드맵 (실행 순서)

### 1. Core Loop
FastAPI + Claude Code CLI 어댑터 + Next.js 기본 챗 UI. 프롬프트를 넣으면 CLI가 서브프로세스로 실행되고 stdout이 WebSocket을 통해 실시간으로 웹 UI에 스트리밍되는 최소 동작 흐름을 완성한다. UI 쪽 세션 상태(브라우저 탭 상태 등)는 메모리에만 유지되며 새로고침하면 사라진다. 단, 대화 자체는 `--resume`/`--continue`로 CLI가 이어가고, 턴별 프롬프트/응답은 transcript 로그 파일로 별도 보관된다(DB는 아직 없음). 이 단계가 나머지 모든 서브프로젝트의 기반이 된다.

**구성 요소**: `packages/adapters/base.py`(추상 인터페이스), `packages/adapters/claude_code.py`, `env_builder`(인증 모드 주입), FastAPI WebSocket 엔드포인트, Next.js(+Tailwind CSS +shadcn/ui) 챗 페이지 1개.

**동시 메시지 처리**: 대장 Agent가 응답 생성 중에 사용자가 새 메시지를 보내면 큐에 쌓아두고 순서대로 처리한다(진행 중인 CLI 프로세스를 중단시키지 않음).

### 2. Memory & State
PostgreSQL(pgvector) 도입. **LangGraph를 지금 실제로 도입**해서 1번 단계의 `ChatSession` 큐잉 구조를 LangGraph `StateGraph` + Postgres 체크포인터로 교체한다(당초 "5번 단계서 진짜 분기 필요할 때 도입" 안이 있었으나, 사용자가 지금 도입 쪽으로 확정). 세션이 끊기거나 서버가 재시작돼도 대화(정확히는 우리 쪽 애플리케이션 상태 — `session_id`, 누적 비용 등)가 이어지고, 과거 턴을 RAG로 검색할 수 있도록 한다.

- **LangGraph 아키텍처**: 대장 Agent는 아직 단일 노드다(sub-agent 분기는 5번 단계에서 추가). `StateGraph`에 노드 하나(`call_lead_agent`)만 두고 `START → call_lead_agent → END`로 컴파일하며, `checkpointer`로 `AsyncPostgresSaver`를 붙인다. 브라우저 연결(웹소켓 세션) 하나당 LangGraph `thread_id` 하나를 부여하고, `config={"configurable": {"thread_id": ...}}`로 매 턴 호출한다. 노드 내부에서 `langgraph.config.get_stream_writer()`로 `ClaudeCodeAdapter.run_stream()`의 각 `AdapterEvent`를 실시간으로 흘려보내고(`stream_mode="custom"`), 턴이 끝나면 최종 상태(`session_id`, 응답 텍스트, 비용)를 반환해 체크포인터가 Postgres에 저장한다. 이 방식은 CLI가 자기 자신의 대화 맥락은 `--resume`으로 들고 있고, LangGraph/Postgres는 "우리 애플리케이션이 그 CLI 세션을 어떻게 다시 찾아가는지"만 영속화하는 역할 분리다. **1번 단계에서 만든 `ChatSession`(`apps/server/app/session_manager.py`)은 이 그래프 기반 구조로 완전히 대체된다.**
- **임베딩 모델**: 원래 계획한 `sentence-transformers`는 이 머신의 사용자 디스크 쿼터(약 31~32GB, 실제 디스크는 여유 있음에도 쿼터로 막힘)를 넘겨 설치 자체가 실패함(실측 확인됨: `pip install`이 `OSError: Disk quota exceeded`로 중단). **`fastembed`(ONNX 기반, PyTorch 의존성 없음)로 대체.** 기본 모델 `BAAI/bge-small-en-v1.5`(384차원) 실측 확인, `.venv` 전체가 328MB로 유지됨. 오프라인/무비용이라는 원래 결정 취지는 동일하게 만족.
- **RAG 코퍼스**: 1번 단계의 `.ana_transcript.jsonl`과 동일한 수준 — 턴별 prompt+response를 그대로 임베딩해 저장(별도 요약 단계 없음). 최소 통합으로, 새 턴을 CLI에 보내기 전에 과거 턴 중 유사도 상위 k개를 검색해 프롬프트에 "관련 과거 대화" 컨텍스트로 덧붙인다.
- **Redis**: 이번 단계에서는 도입하지 않는다(YAGNI). 원래 "세션 단기 캐시" 용도로 기대했던 역할은 LangGraph의 Postgres 체크포인터가 이미 더 튼튼한 형태로 담당한다. 서킷 브레이커용 락/상태는 실제로 서킷 브레이커를 만드는 3번 단계에서, 그때 가서 Redis를 들여온다.
- **Failover 로직**: 대장 Agent가 토큰/사용량 한도로 죽으면 ① 같은 Claude Code CLI의 인증모드 전환(`use-claude-pro` ↔ `use-claude-api`)을 먼저 시도하고, ② 그래도 안 되면 Codex CLI로 전환한다. 1번 단계에서 쌓아둔 transcript 로그 + 이번 단계의 Postgres 체크포인트 상태를 기반으로 대화 맥락을 재구성해 넘겨준다. 원래 쓰던 모드/모델의 사용량이 회복되면 자동으로 원래대로 되돌린다. 이 로직은 3번 단계(Sandbox & Governance)의 서킷 브레이커가 CLI verbose/stats 출력으로 사용량 임계치 초과를 감지했을 때 호출하는 것과 같은 파이프라인이다(서킷 브레이커가 트리거 감지 담당, 이 failover 로직이 실제 전환 담당).

### 3. Sandbox & Governance (통합, 로컬 단일 사용자 기준으로 축소)
샌드박스 격리와 보안 거버넌스를 하나로 묶어 진행한다. 단, 로컬/내부망 단일 사용자 운영이 확정되었으므로 엔터프라이즈 멀티테넌시용 항목(RBAC, Kubernetes, API Gateway)은 제외하거나 아래처럼 축소한다.

- **격리**: 고정 워크스페이스 폴더 → 세션별 독립 워크스페이스 디렉토리 + 세션당 Docker 컨테이너 하나(작업/sub-agent마다 새 컨테이너를 띄우지 않음). 컨테이너 이미지는 최소 구성(Claude Code CLI + Codex CLI만 포함, 다른 도구는 필요해질 때마다 추가). 대장 Agent가 sub-agent를 파생시킬 때 그 sub-agent에 줄 권한(파일 접근 범위, 실행 가능 명령어 등)을 설정할 수 있는 권한 스코프 메커니즘을 같은 컨테이너 안에서 제공한다. Kubernetes 배포/스케일링은 제외(단일 인스턴스로 충분). API Gateway(Kong)도 제외(백엔드가 FastAPI 단일 진입점이라 별도 게이트웨이 불필요).
- **거버넌스**:
  - **HITL 승인 범위**: 외부망 접근이 필요한 모든 작업(API 요청 등 내부망 밖으로 나가는 행동) 또는 위험하다고 판단되는 작업은 반드시 게이트를 통과해야 한다. 게이트 통과 시 먼저 **삭제 불가능한(append-only) 감사 로그**를 무조건 생성하고, 그중 위험하다고 판정된 것만 사용자에게 y/n 승인을 요청한다(CLI 자동 승인을 대체).
  - **Command Interceptor**: 파괴적 명령 차단(Replit의 DB 삭제·결과 은폐 사고, Google Antigravity의 드라이브 전체 삭제 사고를 교훈 삼음). 위 HITL 게이트의 "위험 판정" 로직을 담당. 차단 규칙은 **YAML 설정 파일**(화이트리스트/블랙리스트 패턴 목록)로 정의해 코드 수정 없이 규칙 추가·삭제 가능하게 한다. 초기 차단 카테고리:
    - 파일시스템 파괴: `rm -rf`, 워크스페이스 밖 경로 삭제/덮어쓰기, 디스크 포맷 명령
    - DB 파괴: `DROP TABLE`/`DROP DATABASE`, `WHERE` 없는 `DELETE`/`UPDATE`, `TRUNCATE`
    - Git 이력 파괴: `push --force`(공유 브랜치), `reset --hard` + `clean -f` 조합, 히스토리 재작성
    - 시스템 제어: `shutdown`/`reboot`, 컨테이너 밖 프로세스 대상 `kill`
    - 자격증명 유출 시도: 시크릿 값을 포함한 외부 전송(위 유출 방지 항목과 연동), `.env`/키 파일 자체를 읽어 외부로 보내는 시도
    - 은폐성 행동: 실패를 감추기 위해 로그/테스트 결과를 조작하는 패턴(Replit 사고 대응)
  - **시크릿 관리 & 유출 방지**: 프로젝트별/글로벌 범위로 환경변수(API 키, GitHub 토큰, DB 비밀번호 등)를 등록·관리하는 웹 UI를 제공(1번 단계의 파일 기반 `.env`를 이 단계에서 정식 관리 화면으로 확장). Command Interceptor가 외부망으로 나가는 요청/응답 내용을 스캔해 등록된 시크릿 값이 포함되어 있으면 전송 자체를 차단하거나 마스킹한다. 이 유출 방지 검사는 위 HITL 게이트(외부망 접근 작업)와 같은 지점에서 동작한다.
  - **서킷 브레이커**: 토큰/비용 임계치를 기준으로 트리거한다. CLI의 verbose/stats 출력을 파싱해 사용량을 추정하고, 임계치를 넘으면 2번 단계(Memory & State)에서 정의한 failover 메커니즘을 그대로 호출해 대화 맥락을 유지한 채 다른 인증모드/모델로 전환한다(즉 서킷 브레이커와 failover는 같은 파이프라인).
  - **STRIDE 체크리스트**(이 시스템 맥락으로 매핑):
    - Spoofing(위장): 내부망 신뢰 전제이므로 우선순위 낮음. sub-agent가 대장을 사칭해 명령을 내리지 못하도록 세션 간 프로세스 격리로 방지.
    - Tampering(변조): Command Interceptor가 워크스페이스 밖 파일 변조, DB 직접 변조 차단.
    - Repudiation(부인 방지): append-only 감사 로그로 모든 행동 기록(누가/언제/무엇을 승인했는지 추적 가능).
    - Information Disclosure(정보 노출): 시크릿 유출 방지 스캔으로 대응.
    - Denial of Service(서비스 거부): 서킷 브레이커(토큰/비용 임계치) + sub-agent 동시 실행 상한(5개)으로 리소스 고갈 방지.
    - Elevation of Privilege(권한 상승): sub-agent는 대장이 부여한 권한 스코프를 못 벗어남(컨테이너 내 스코프 메커니즘).
  - **MAESTRO 체크리스트**(시스템 생애주기 관점): 개발 단계(Command Interceptor 규칙·시크릿 스캔 룰 리뷰) → 배포 단계(docker-compose로 구성요소 버전 고정, 이미지 최소화) → 운영 단계(감사 로그·서킷 브레이커 지표를 4번 Observability에서 상시 관찰) → 폐기/롤백 단계(세션 컨테이너는 종료 시 삭제, 워크스페이스만 잔존해 언제든 재현 가능).
  - RBAC는 역할 기반 접근 제어 대신 "명령어 화이트리스트/블랙리스트" 수준으로 축소(사용자가 한 명이므로 역할 구분이 아니라 허용 동작 범위 제어가 목적).

두 영역을 묶는 이유: 격리 없는 승인 체계나 승인 없는 격리 체계 모두 반쪽짜리 안전장치이기 때문에, 같은 스프린트에서 함께 검증한다.

### 4. Observability
Langfuse는 **self-hosted(Docker)**로 배포(내부망 전용 운영 방침과 일치, 외부로 트레이스 데이터 안 나감)하여 에이전트 실행 트레이싱(단계별 지연시간, 토큰 비용, 프롬프트/응답 기록)을 수행. Prometheus + Grafana로 인프라 지표(컨테이너 자원, API 성공률, 서킷 브레이커 발동 횟수) 모니터링. **데이터 보존 기간: 무제한**(일단 자동 삭제 없이 전부 보관, 용량 문제 생기면 그때 정책 도입).

### 5. Multi-Agent Orchestration
Core Loop에서 만들어둔 어댑터 인터페이스에 Codex CLI 어댑터를 실제로 구현해 추가. 대장 Agent(1번 단계에서 만든 persistent 세션)가 세부 작업이 필요할 때 새로운 sub-agent 세션(별도 CLI 프로세스)을 동적으로 파생시키고, 완료되면 결과를 회수하는 Planner-Worker 구조를 구현한다. sub-agent는 대장과 **같은 워크스페이스·같은 컨테이너(3번에서 정한 세션당 컨테이너 하나)를 공유**하며, 대장이 부여한 권한 스코프 안에서 동작한다. **모델 선택**: sub-agent마다 Claude Code CLI를 쓸지 Codex CLI를 쓸지는 고정하지 않고, 대장 Agent가 작업 성격을 보고 동적으로 판단한다(예: 코드 생성/리팩토링류는 Codex, 설명·리뷰·기획류는 Claude — 정확한 판단 규칙은 이 단계 구현 시 프롬프트로 대장에게 위임). **동시 실행 개수**: 설정값으로 상한을 두며 기본값 5(리소스 폭주 방지, 필요 시 조정 가능). Supervisor 패턴도 함께 구현. React Flow로 대장 Agent와 sub-agent 간 흐름을 웹 UI에 시각화.

### 6. MCP & Skills
**기존 오픈소스 MCP 서버 재사용을 우선**한다. 우선순위: ① **Git/GitHub**(커밋·PR·이슈 관리 — 에이전트 기본 워크플로에 거의 필수), ② **Postgres/DB**(2번 단계에서 구축한 메모리·상태 DB를 에이전트가 직접 조회). 파일시스템·웹 검색 등은 필요해지면 추후 추가. 사내 전용 API 연동처럼 대체재가 없는 경우에만 `packages/mcp_servers/`에서 직접 개발.

**스킬(Skill) 정의**: YAML 포맷으로 `packages/skills/`에 정의(Claude Code 자체 스킬 관례와 유사). Pydantic으로 입출력 스키마 검증. 에이전트 스킬 라이브러리를 구축하고, Planner/Worker 등 역할별로 MCP 접근 권한을 분리(Scope Partitioning).

## 의존 관계

- **1번(Core Loop)**은 필수 기반이며 2~6번 전부 그 위에 얹힌다.
- **3번(Sandbox & Governance)**은 격리와 보안을 함께 검증하기 위해 묶어서 진행한다.
- **5번(Multi-Agent Orchestration)**은 1번의 어댑터 인터페이스를 확장하는 형태로 진행한다.
- **4번(Observability)**과 **6번(MCP & Skills)**은 1~3번 완료 후 순서 제약이 크지 않지만, 표기된 순서를 기본 권장안으로 삼는다.

## 운영(실행 방식)

전체 스택(FastAPI, Next.js, PostgreSQL, Redis, Langfuse, Docker 샌드박스 등)은 **docker-compose 하나로 통합** 관리한다. `docker-compose up`으로 전체 기동, `down`으로 종료. 1번 단계(Core Loop)는 FastAPI + Next.js만으로 구성됐고, 2번 단계(Memory & State)에서 PostgreSQL(pgvector)이 compose 파일에 추가된다. Redis는 2번 단계에서 도입하지 않기로 했으므로(YAGNI, 위 참고) 3번 단계(Sandbox & Governance)에서 서킷 브레이커와 함께 추가된다.

## 현재 상태

1번(Core Loop) 설계 완료, 사용자 승인 대기 중. 승인 후 구현 계획(implementation plan) 작성 및 착수.
