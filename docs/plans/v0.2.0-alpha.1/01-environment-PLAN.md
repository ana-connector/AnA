# v0.2.0-alpha.1 환경·테스트 기반 계획

상태: **구현됨**. 환경 복구와 기준선 확보는 끝났다. 남은 제품 실패는 [02-stability-PLAN.md](02-stability-PLAN.md)가 맡는다. 실제로 확인하고 고친 내용은 아래 "실행 기록"에 있다.

## 실행 기록

### 진단 (1단계)

테스트는 **컨테이너 안**(`/.dockerenv` 존재, `ana_default` 네트워크의 `172.25.0.2`)에서 돌지만, Docker CLI는 마운트된 소켓으로 **호스트 데몬**(Ubuntu 26.04, Docker 29.7.1)에 붙는다. 그래서 `docker run -p <포트>:5432`로 공개한 포트는 호스트에 열리고, 테스트 프로세스의 `localhost`로는 닿지 않는다. 같은 `localhost`가 두 네트워크를 가리킨 것이 DB 접속 거부의 원인이었다.

### 고친 것

1. **`.venv` 복구**: `.venv/bin/python3`이 사라진 인터프리터를 가리키고 있었다. 설치된 패키지(site-packages 145개)는 그대로 두고 인터프리터 심링크와 `pyvenv.cfg`만 다시 연결했다. 요구사항 설치와 수집이 재현된다.
   - 이 고장은 **두 번** 일어났다. 처음에는 `~/.local/share/mise/.../3.13.14`가 사라져 `/usr/local/bin/python3.13`(3.13.16)으로 옮겼고, 2026-10-16에는 `/usr/local/bin`이 통째로 사라져 다시 mise의 3.13.14로 옮겼다. 두 경로 모두 컨테이너 재생성에서 살아남지 않는다. 베이스 인터프리터가 사라지면 `.venv/bin/python`은 `No such file or directory`로 죽고 `timeout`조차 실행에 실패하므로, 테스트가 전부 exit=127이면 이것부터 의심한다. 복구는 `.venv/bin/python3` 심링크와 `.venv/pyvenv.cfg`의 `home`/`executable`/`version`을 현존하는 3.13.x로 바꾸면 끝난다.
2. **`uvx` 복구**: `config/mcp.yaml`의 `git`과 `postgres` 서버는 `uvx`로 뜨고, [registry.py](../../../packages/mcp/registry.py)는 `shutil.which`로 없는 명령을 조용히 비활성화한다. `uvx`가 PATH에서 사라지자 `/mcp/servers?role=lead`의 active 목록에서 두 서버가 빠져 `test_capabilities_endpoint.py`가 실패했다. `.venv`에 `uv`를 설치하고 `~/.local/bin/uv(x)`로 링크해 복구했다. 제품 결함이 아니라 환경 결손이다.
3. **DB 픽스처**([conftest.py](../../../apps/server/tests/conftest.py))
   - 컨테이너 안에서 돌면 테스트 DB를 **같은 Docker 네트워크에 붙이고** 컨테이너 주소로 접속한다. 포트를 아예 공개하지 않으므로 호스트에 노출되는 것이 없다.
   - 호스트에서 돌면 `127.0.0.1`에만 공개한다. 이전에는 바인딩 주소를 지정하지 않아 모든 인터페이스에 열렸다.
   - 준비 확인을 컨테이너 안의 `pg_isready`에서 **테스트 프로세스가 실제 DSN으로 여는 접속**으로 바꿨다. 초기화 중 Postgres가 한 번 재시작하므로 이른 성공은 근거가 되지 않는다. 컨테이너가 죽으면 로그와 함께 즉시 실패한다.
   - 접속 후 `current_database()`가 `ana_test`인지 검사한다. 아니면 마이그레이션 전에 중단한다.
   - Docker 데몬에 닿을 수 없는 실행기를 위해 `ANA_TEST_DATABASE_URL` 우회를 뒀다. 이 경우에도 DB 이름 검사는 적용된다.
4. **잔해 정리**: 환경이 깨져 있는 동안 남은 `ana-test-pg-*` 컨테이너 5개(하나는 2주째 실행 중이라 포트를 쥐고 있었다)를 제거했다.

### 검증

`PGHOSTADDR` 같은 환경 변수 없이 `test_memory_db.py` 3개가 통과하고, 실행 뒤 테스트 컨테이너가 남지 않는다. 웹 쪽은 Node 20으로 Dockerfile(`node:20-slim`)과 일치하며 타입 검사와 린트가 실행된다.
후속 Plan02 검증도 같은 일회용 DB 픽스처로 파일별 순차 실행했다. 최신 결과와 단일 프로세스 OOM 제약은 [02-baseline-RESULTS.md](02-baseline-RESULTS.md)에 기록했다. 개발·운영 DB와 볼륨은 검사에 사용하지 않았다.

---

아래는 작업 전에 작성한 원래 계획이다.

## 목표와 범위

- Python 의존성을 새 체크아웃에서 재현 가능하게 설치하고, 서버 테스트를 같은 인터프리터로 실행한다.
- 테스트 전용 일회용 `pgvector` PostgreSQL에 테스트 실행 위치에서 접속할 수 있게 한다.
- 전체 서버 테스트의 기준 결과를 남겨 이후 안정화 작업의 실패를 환경 오류와 제품 오류로 구분한다.
- 범위는 [제품 로드맵](../v1.0.0-product-roadmap.md)의 **v0.2.0-alpha.1** 준비 단계다. 데몬이 호스팅하는 웹 UI는 하나이며, Windows·Debian·Android는 훗날 이를 여는 얇은 셸이다. 단일 출처 전환과 셸 구현은 이 단계의 작업이 아니다.

## 범위 밖

- 실패한 제품 동작의 기대값 변경, Kimi 페일오버·입력 지연 로직 수정, UI 또는 CI 구현.
- 운영 DB, 기존 Compose 볼륨, 개발자의 실제 AnA 메모리·워크스페이스 데이터로 테스트 실행.
- 새 서비스나 장기 보관 DB를 테스트의 필수 조건으로 만드는 일.

## 현재 코드에서 확인한 사실과 미확인 항목

1. [README.md](../../../README.md)의 테스트 절은 `.venv` 생성, `apps/server/requirements.txt` 설치, `.venv/bin/python -m pytest`를 안내한다. [pyproject.toml](../../../pyproject.toml)은 테스트 경로를 `apps/server/tests`로 지정한다. 서버 이미지도 [apps/server/Dockerfile](../../../apps/server/Dockerfile)에서 Python 3.13과 같은 요구사항 파일을 사용한다.
2. 현재 이 작업 트리에는 `.venv`가 없다. 확인 당시 `python3`는 3.13.16이었고 Docker CLI는 데몬에 연결됐다. 이것은 의존성 설치나 DB 접속 성공의 증거가 아니다.
3. [apps/server/tests/conftest.py](../../../apps/server/tests/conftest.py)의 `postgres_dsn`은 임의 호스트 포트로 `pgvector/pgvector:pg16` 컨테이너를 띄우고 `localhost:<port>/ana_test`를 돌려준다. 준비 확인은 **컨테이너 안**의 `pg_isready`로 수행한다. 현재 `-p <port>:5432`는 바인딩 주소를 지정하지 않는다.
4. [packages/memory/db.py](../../../packages/memory/db.py)의 `create_pool`은 해당 DSN으로 실제 TCP 연결을 열고 `vector` 확장을 준비한다. 짧은 재시도는 있지만, 컨테이너 안의 준비 상태와 테스트 프로세스의 포트 도달 가능성은 별개의 조건이다.
5. [docker-compose.yml](../../../docker-compose.yml)의 개발용 Postgres는 `127.0.0.1:15432:5432`로 공개되고 별도 영속 볼륨을 사용한다. [.env.example](../../../.env.example)의 `DATABASE_URL`도 그 개발용 DB를 가리킨다. 테스트는 이 주소나 Compose 볼륨을 사용해서는 안 된다.
6. 테스트는 `db_pool`에서 마이그레이션을 실행하고 `turns`를 비운다. `test_memory_db.py`, `test_main.py`, `test_failover.py` 등이 이 픽스처에 의존한다. 잘못된 DSN을 넘기면 데이터 손실 위험이 있다.
7. 이전 작업자가 전달한 “비 DB 테스트 80개 통과, 872개 수집, DB 접속 거부, `.venv` 문제”는 **재검증 전 참고 정보**다. 현재 작업에서 전체 테스트를 다시 돌려 확인한 수치가 아니다.

## 실행 순서

### 1. 실행 경계와 안전한 테스트 대상 식별

- 입력: Docker context·daemon endpoint, 테스트 프로세스의 실행 위치, 현재 `conftest.py`, 사용 중인 포트.
- `docker context inspect`, `docker info`, `docker ps`, 테스트 프로세스 쪽의 `127.0.0.1` 네트워크를 확인한다. Docker CLI가 접속하는 데몬이 로컬인지, 컨테이너 밖 호스트인지 기록한다. 비밀 값이 들어 있는 `.env` 전체나 DSN 암호는 로그에 출력하지 않는다.
- 임시 컨테이너의 실제 공개 주소와 호스트 포트를 `docker port <일회용 컨테이너>`로 읽고, **테스트 프로세스가 실행되는 곳에서** 그 주소로 TCP·`psycopg` 접속을 확인한다. 컨테이너 내부 `pg_isready`만으로 통과 처리하지 않는다.
- 출력: 실행 위치 → Docker daemon → 공개 포트 → 테스트 프로세스의 접속 경로를 적은 짧은 진단 기록. 환경별로 같은 `localhost`가 서로 다른 네트워크를 뜻하면 그 차이를 명시한다.
- 완료 기준: 테스트 프로세스에서 `ana_test`의 일회용 DB 접속이 성공하고, 사용 DB 이름·컨테이너 식별자·정리 절차를 확인했다.

### 2. Python 환경 재현

- 입력: Python 3.13, [apps/server/requirements.txt](../../../apps/server/requirements.txt), 기존 `.venv`의 존재·정상 여부.
- 새 가상 환경을 만들고 `python -m pip install -r apps/server/requirements.txt`를 실행한다. 기존 환경이 있다면 인터프리터 경로, `pip --version`, 필수 패키지 import를 먼저 확인하고 손상된 경우에만 해당 작업 트리의 가상 환경을 재생성한다.
- `python -m pytest --collect-only -q`로 수집 단계의 import 실패를 분리한다. 모든 명령은 선택한 가상 환경의 `python -m ...` 형식으로 실행해 시스템 `pytest`와 혼용하지 않는다.
- 출력: 재현 명령, Python·pip 버전, 설치와 수집 결과. 설치 실패 시 패키지 이름과 오류를 기록하고 제품 테스트 실패로 분류하지 않는다.
- 완료 기준: 깨끗한 작업 트리에서 요구사항 설치와 테스트 수집이 재현된다.

### 2-1. 웹 검증 환경 재현

- `apps/web/package.json`, lockfile, Dockerfile의 Node 버전을 대조하고 같은 버전에서 `npm ci`를 실행한다.
- 설치·타입 검사 실행 가능 여부를 기록한다. 린트의 제품 오류 수정은 02, CI 구성은 05가 담당한다.
- 출력: Node/npm 버전, 의존성 설치 결과, 웹 검사 실행 명령. 버전·다운로드 문제를 제품 실패와 구분한다.

### 3. 일회용 PostgreSQL 픽스처 안정화

- 입력: 1단계의 네트워크 진단과 [apps/server/tests/conftest.py](../../../apps/server/tests/conftest.py).
- 기본 경로에서는 테스트 컨테이너 포트를 loopback에만 바인딩한다. 준비 확인에 실제 DSN으로 접속하는 검사를 추가하고, 컨테이너 초기화 중 발생할 수 있는 잠깐의 연결 거부를 제한된 시간 동안 재시도한다. Docker daemon이 다른 호스트에 있으면 포트가 공개되는 **daemon 호스트 주소**와 테스트 실행 위치의 접근 가능성을 명시적으로 결정한다.
- 자유 포트 탐색과 `docker run` 사이의 경쟁, 컨테이너 생성 실패, 준비 시간 초과, 테스트 예외 모두에서 오류 메시지와 컨테이너 정리를 확인한다. 테스트 컨테이너 이름은 충돌하지 않게 유지한다.
- 확인용으로 일회용 DB 안에서 `SELECT current_database()`와 `SELECT 1`을 실행한다. 결과가 `ana_test`가 아니면 즉시 중단한다. `DATABASE_URL`이나 개발용 `127.0.0.1:15432/ana`로 대체하지 않는다.
- 출력: 작은 픽스처 수정과 그 수정의 근거가 된 접속 진단. 환경 차이로 설정값이 필요하다면 안전한 기본값과 사용 예를 함께 문서화한다.
- 완료 기준: `test_memory_db.py`의 마이그레이션·벡터 왕복 테스트를 새 일회용 DB에서 반복 실행해 접속 실패 없이 끝내고, 실행 뒤 컨테이너가 제거된다.

### 4. 전체 서버 테스트 기준선 확정

- 입력: 재현된 Python 환경과 검증된 일회용 DB 경로.
- `.venv/bin/python -m pytest apps/server/tests -q`를 실행하고 전체 수집 수, 통과·실패·오류·건너뜀 수, 소요 시간, 첫 실패의 traceback을 기록한다. 테스트가 사용하는 임시 상태·워크스페이스 경로가 실제 사용자 데이터와 분리됐는지 [conftest.py](../../../apps/server/tests/conftest.py)의 자동 픽스처로 확인한다.
- 연결·설치 오류가 사라진 뒤 남는 assertion·동작 실패는 이 단계에서 고치지 않고 [02-stability-PLAN.md](02-stability-PLAN.md)에 파일·테스트명·재현 명령과 함께 전달한다. 비결정적 실패는 같은 테스트만 재실행해 조건을 좁힌다.
- 출력: 재현 가능한 명령과 성공/실패 기준선, 제품 실패 목록. 과거의 80/872 수치는 새 실행 결과로 대체하거나 명확히 미검증으로 남긴다.
- 완료 기준: 전체 테스트가 실제로 실행 가능하고 환경 오류와 제품 실패가 구별된다. 제품 실패가 남아도 그 사실과 소유 단계를 기록하면 환경 단계의 진단은 완료된다.

## 검증 명령과 합격 기준

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r apps/server/requirements.txt
.venv/bin/python -m pytest --collect-only -q
.venv/bin/python -m pytest apps/server/tests/test_memory_db.py -q
.venv/bin/python -m pytest apps/server/tests -q
```

- 이 명령은 **실행 예정**이며 이 문서 작성 중 실행한 결과가 아니다. DB 연결 검증은 픽스처가 만든 `ana_test`에 한정한다.
- 설치·수집·DB 스모크·전체 테스트의 결과와 실행 환경을 남겨 다른 작업자가 같은 실패를 재현할 수 있어야 한다.
- `test_memory_db.py`에서 `vector` 확장 생성과 벡터 저장·읽기가 성공해야 한다. 전체 테스트의 제품 assertion까지 이 단계에서 무조건 녹색으로 만드는 것은 목표가 아니다.

## 다른 계획과의 의존 관계

- 이 문서가 1단계다. [02-stability-PLAN.md](02-stability-PLAN.md)는 여기서 확인한 제품 실패 기준선과 안정적인 DB 픽스처를 입력으로 사용한다.
- [03-design-PLAN.md](03-design-PLAN.md)의 디자인 탐색은 환경 복구 및 안정화와 병행할 수 있다. [04-ui-independence-PLAN.md](04-ui-independence-PLAN.md)의 적용·회귀 검증은 디자인 결정과 안정된 기존 동작을 전제로 한다.
- [05-release-PLAN.md](05-release-PLAN.md)의 CI 기반 시설 작업은 일찍 시작할 수 있지만 alpha.1 출시 판정은 환경·안정화·UI 적용의 검증 근거를 받은 뒤에 한다.

## 위험과 되돌리기

- 원격 Docker daemon 또는 중첩 컨테이너에서는 `localhost` 공개 포트가 테스트 프로세스에 닿지 않을 수 있다. 실제 경로를 먼저 진단하고, 접근 가능한 명시 주소나 테스트 실행 위치 조정 중 작은 변경을 선택한다.
- 잘못된 DSN은 운영·개발 DB에 마이그레이션이나 `TRUNCATE`를 수행할 수 있다. 컨테이너 이름과 DB 이름을 고정된 안전 조건으로 검사하고, 테스트 외부 DB로의 우회는 허용하지 않는다.
- 포트 노출과 남은 컨테이너는 별도 위험이다. loopback 바인딩, 항상 실행되는 정리 절차, 종료 후 `docker ps` 확인으로 제한한다.
- 픽스처 변경으로 새 환경에서 회귀가 생기면 이전 픽스처 구현으로 되돌리고 진단 기록을 보존한다. 일회용 컨테이너만 제거하며 Compose 볼륨에는 손대지 않는다.

## 사용자에게만 남길 결정

- 현재 필수 결정은 없다. 만약 테스트 실행기가 Docker daemon 호스트로 접근할 수 없는 배치라면, 1단계 진단 후 **CI 실행 위치 또는 전용 테스트 DB 제공 방식**에 대한 운영 선택만 사용자에게 제시한다. 운영 DB 사용 승인은 선택지에 넣지 않는다.

## 바로 시작할 첫 작업

테스트가 실제로 실행될 위치에서 Docker context·daemon 주소와 일회용 컨테이너의 공개 포트 연결을 함께 진단한다. 결과를 기록한 뒤 Python 가상 환경을 설치한다.
