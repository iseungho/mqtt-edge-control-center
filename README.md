# MQTT Edge Control Center

MQTT 기반 엣지 장비 5개의 상태를 모니터링하고 원격 명령을 비동기로 실행하는 Docker Compose 프로젝트입니다. QoS 1 중복 전달, 서버·Agent 재시작, Broker 연결 중단, 인증 실패와 Agent별 Topic 권한을 실제 장비 관리 환경의 핵심 장애 조건으로 다룹니다.

## 빠른 실행

필요 환경은 Docker Desktop과 Docker Compose입니다.

```powershell
Copy-Item .env.example .env
```

`.env`의 여섯 비밀번호를 모두 변경한 뒤 실행합니다. `.env`는 Git에서 제외됩니다.

```powershell
docker compose up -d --build
```

- Dashboard: <http://localhost:8000>
- REST API 문서: <http://localhost:8000/docs>
- MQTT: `localhost:1883` (인증 필수)

`broker-auth`는 계정별 비밀번호 파일을 만든 뒤 `Exited (0)`으로 종료하는 초기화 컨테이너입니다. 실제 MQTT 서비스는 `broker`가 계속 실행하며 정상 상태는 `healthy`입니다.

종료 시 데이터는 named volume에 남습니다.

```powershell
docker compose down
```

상태와 이력까지 초기화하려는 경우에만 `docker compose down -v`를 사용합니다.

## 시스템 구조

```text
Browser
  │ REST 조회·명령
  ▼
FastAPI API ── SQLite (Agent 상태·명령 이력)
  │
  │ MQTT QoS 1 / control-server 계정
  ▼
Mosquitto ── Password 인증 + ACL + Broker persistence
  │
  ├── agent-a ── SQLite (처리한 Request ID·설정)
  ├── agent-b ── SQLite
  ├── agent-c ── SQLite
  ├── agent-d ── SQLite
  └── agent-e ── SQLite
```

서버는 REST, MQTT 연결, 메시지 검증, 상태 저장을 각각 분리합니다. Agent는 MQTT Loop와 명령 실행·중복 방지를 분리합니다.

## 개선 전후

| 구분 | 개선 전 | 개선 후 |
| --- | --- | --- |
| Agent 등록 | 서버에 A/B/C 하드코딩 | 유효한 Heartbeat 수신 시 자동 등록 |
| 서버 상태 | 메모리, 재시작 시 초기화 | SQLite와 Docker volume에 영속화 |
| 명령 상태 | PENDING → SUCCESS/TIMEOUT | PENDING → RUNNING → SUCCESS/FAILED/TIMEOUT |
| 중복 명령 | 재실행 가능 | Agent SQLite의 Request ID UNIQUE로 실행 1회 보장 |
| 명령 종류 | RESTART | RESTART, GET_SYSTEM_INFO, GET_CONFIG, SET_HEARTBEAT_INTERVAL |
| 재연결 | 기본 재연결 | 1~10초 backoff, 연결마다 재구독, 완료 응답 재전송 |
| Broker | 익명 접속 | 사용자 인증, Agent별 ACL, persistence |
| 실행 Agent | 3개 | 동일 이미지 기반 5개 |

## 프로젝트 구조와 책임

```text
agent/
├─ agent.py          # MQTT 연결·재연결, Heartbeat, 응답 발행
├─ commands.py       # 명령 검증·실행, Request ID/설정 SQLite 저장
└─ test_agent.py
server/
├─ app.py            # FastAPI endpoint
├─ mqtt_service.py   # MQTT 연결·재구독·메시지 routing
├─ protocol.py       # Topic과 JSON trust-boundary 검증
├─ storage.py        # Agent/명령 SQLite 저장과 상태 전이
├─ static/           # Dashboard
└─ test_app.py
mosquitto/
├─ mosquitto.conf    # 인증·ACL·persistence 설정
└─ acl               # 서버와 Agent별 Topic 권한
tests/
└─ integration.ps1   # 인증, ACL, 중복, 재시작, 재연결 통합 검증
docker-compose.yml   # Broker, Server, Agent 5개와 영속 volume
```

새 명령은 `agent/commands.py`의 지원 목록과 실행 분기에 추가하고 서버의 `protocol.py` 허용 목록에 등록합니다. 새 Agent는 별도 Agent 코드를 만들지 않고 같은 이미지를 `AGENT_ID`, 계정, 독립 volume으로 실행합니다. 서버 코드는 수정하지 않습니다.

## 명령 상태와 장애 의미

| 상태 | 의미 |
| --- | --- |
| `PENDING` | 서버가 이력을 먼저 저장하고 Broker에 발행함 |
| `RUNNING` | Agent가 Request ID를 저장하고 실행을 시작함 |
| `SUCCESS` | Agent가 결과를 생성했고 서버가 응답을 수신함 |
| `FAILED` | Agent 실행 실패 또는 발행 실패 사유가 확인됨 |
| `TIMEOUT` | 10초 안에 최종 결과를 받지 못해 성공 여부를 확정할 수 없음 |

`TIMEOUT`은 실패 확정이 아닙니다. 실제 명령이 실행된 직후 네트워크가 끊겼을 수도 있으므로 운영자가 이력과 장비 상태를 확인해야 합니다. 늦게 도착한 응답은 이미 확정한 TIMEOUT을 덮어쓰지 않습니다.

RESTART는 컨테이너 자체를 종료하지 않고 `{ "message": "restart simulated" }` 결과를 반환합니다. 접수와 실행 시작, 완료 응답은 실제 명령과 동일한 상태 흐름으로 분리됩니다.

## MQTT Topic과 ACL

| Topic | 발행 | 구독 | QoS |
| --- | --- | --- | :---: |
| `agents/{agent_id}/heartbeat` | 해당 Agent | Server | 1 |
| `agents/{agent_id}/commands` | Server | 해당 Agent | 1 |
| `agents/{agent_id}/responses` | 해당 Agent | Server | 1 |

`control-server`는 모든 Agent의 Heartbeat/응답을 읽고 명령만 발행할 수 있습니다. Agent 계정은 Mosquitto ACL의 `%u` 패턴을 사용하므로 자신의 `agents/{username}/...` Topic만 접근합니다. 예를 들어 `agent-a`가 `agents/agent-b/heartbeat`에 발행하면 Broker가 거부합니다.

Broker 재연결 시 Server와 Agent의 `on_connect`가 필요한 Topic을 다시 구독합니다. Agent는 로컬 SQLite에 저장한 최근 완료 응답을 다시 발행하여, 서버 재시작이나 응답 전달 중단 뒤에도 같은 Request ID의 결과를 동기화합니다.

### Heartbeat

```json
{
  "agent_id": "agent-a",
  "timestamp": "2026-11-06T00:00:00+00:00",
  "cpu_usage": 35.2,
  "memory_usage": 61.7,
  "disk_usage": 92.4
}
```

서버는 Topic/Payload Agent ID 일치, 시간대, 사용률 타입과 0~100 범위를 검증합니다. Agent가 보낸 시각이 아니라 서버 수신 시각의 monotonic clock을 기준으로 30초 동안 Heartbeat가 없으면 OFFLINE입니다. 서버 재시작 직후에는 SQLite에 저장한 서버 수신 시각을 사용하고, 첫 Heartbeat부터 monotonic clock으로 전환합니다.

### 명령 요청

```json
{
  "request_id": "73d215f2-50ce-4257-9f96-ae04afc37de5",
  "command": "SET_HEARTBEAT_INTERVAL",
  "payload": { "interval": 15 },
  "status": "PENDING",
  "requested_at": "2026-11-06T00:00:00+00:00"
}
```

### 명령 응답

실행 시작:

```json
{
  "request_id": "73d215f2-50ce-4257-9f96-ae04afc37de5",
  "agent_id": "agent-a",
  "command": "SET_HEARTBEAT_INTERVAL",
  "status": "RUNNING",
  "result": null,
  "error": null,
  "responded_at": "2026-11-06T00:00:01+00:00"
}
```

완료:

```json
{
  "request_id": "73d215f2-50ce-4257-9f96-ae04afc37de5",
  "agent_id": "agent-a",
  "command": "SET_HEARTBEAT_INTERVAL",
  "status": "SUCCESS",
  "result": { "heartbeat_interval": 15.0 },
  "error": null,
  "responded_at": "2026-11-06T00:00:02+00:00"
}
```

같은 Request ID가 다시 전달되면 Agent는 명령을 실행하지 않고 SQLite에 저장된 응답만 재전송합니다. Agent가 RUNNING 중 종료되면 재시작 시 해당 건을 FAILED로 바꾸고 실패 사유를 재전송합니다.

## REST API

| Method | Path | 설명 |
| --- | --- | --- |
| `GET` | `/api/agents` | 자동 등록된 모든 Agent와 최신 명령 |
| `GET` | `/api/commands?agent_id=agent-a&limit=100` | 영속 명령 이력과 결과/실패 사유 |
| `POST` | `/api/agents/{id}/commands` | 범용 원격 명령, HTTP 202 |
| `POST` | `/api/agents/{id}/restart` | 기존 RESTART API 호환 경로 |

명령 예시:

```powershell
$body = @{ command = "GET_SYSTEM_INFO"; payload = @{} } | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType application/json -Body $body http://localhost:8000/api/agents/agent-a/commands
```

Heartbeat 변경 범위는 1~3600초입니다.

```powershell
$body = @{ command = "SET_HEARTBEAT_INTERVAL"; payload = @{ interval = 15 } } | ConvertTo-Json
Invoke-RestMethod -Method Post -ContentType application/json -Body $body http://localhost:8000/api/agents/agent-a/commands
```

## 검증

단위 테스트:

```powershell
docker compose run --rm server python -m unittest -v
docker compose run --rm agent-a python -m unittest -v
```

통합·장애 테스트:

```powershell
powershell -ExecutionPolicy Bypass -File tests/integration.ps1
```

통합 스크립트는 다음을 자동 검증합니다.

1. Agent 5개가 동시에 ONLINE
2. 잘못된 비밀번호의 Broker 접속 거부
3. Agent A의 Agent B Topic 발행 거부
4. 동일 Request ID를 두 번 발행해도 Agent DB 처리 건수 1개
5. Server 재시작 뒤 명령 이력 유지
6. Broker 재시작 뒤 자동 재연결·재구독과 Heartbeat 복구

### 통합 테스트 결과

2026-11-06 로컬 Docker 환경에서 통합 스크립트를 실행했습니다.

| 검증 항목 | 결과 |
| --- | :---: |
| Agent 5개 동시 ONLINE | PASS |
| 잘못된 MQTT 비밀번호 접속 거부 | PASS |
| 다른 Agent Topic 발행 ACL 거부 | PASS |
| 동일 Request ID 중복 실행 방지 | PASS |
| Server 재시작 후 명령 이력 유지 | PASS |
| Broker 재시작 후 재연결·재구독 | PASS |
| 최종 Broker healthy, Agent 5/5 ONLINE | PASS |

수동 TIMEOUT 확인:

```powershell
docker compose stop agent-c
Invoke-RestMethod -Method Post http://localhost:8000/api/agents/agent-c/restart
Start-Sleep -Seconds 11
Invoke-RestMethod "http://localhost:8000/api/commands?agent_id=agent-c"
docker compose start agent-c
```
