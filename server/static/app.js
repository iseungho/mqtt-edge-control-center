const rows = document.querySelector("#agent-rows");
const connection = document.querySelector("#connection");
const updatedAt = document.querySelector("#updated-at");
const totalCount = document.querySelector("#total-count");
const onlineCount = document.querySelector("#online-count");
const warningCount = document.querySelector("#warning-count");

function addCell(row, value, className) {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = value;
  row.append(cell);
}

function addBadge(row, value) {
  const cell = document.createElement("td");
  const badge = document.createElement("span");
  badge.className = `badge ${value.toLowerCase()}`;
  badge.textContent = value;
  cell.append(badge);
  row.append(cell);
}

function addCommand(row, agent) {
  if (agent.command) {
    addBadge(row, agent.command.status);
  } else {
    addCell(row, "—");
  }

  const cell = document.createElement("td");
  const button = document.createElement("button");
  button.className = "restart-button";
  button.type = "button";
  button.textContent = "RESTART";
  button.disabled = agent.command?.status === "PENDING";
  button.setAttribute("aria-label", `${agent.agent_id} 재시작 명령`);
  button.addEventListener("click", () => restartAgent(agent.agent_id, button));
  cell.append(button);
  row.append(cell);
}

function formatMetric(value) {
  return value === null ? "—" : `${Number(value).toFixed(1)}%`;
}

function formatHeartbeat(value) {
  return value ? new Date(value).toLocaleString("ko-KR", { hour12: false }) : "수신 대기 중";
}

function render(agents) {
  rows.replaceChildren();

  for (const agent of agents) {
    const row = document.createElement("tr");
    addCell(row, agent.agent_id, "agent-id");
    addBadge(row, agent.status);
    addCell(row, formatHeartbeat(agent.last_heartbeat));
    addCell(row, formatMetric(agent.cpu_usage), "metric");
    addCell(row, formatMetric(agent.memory_usage), "metric");
    addCell(row, formatMetric(agent.disk_usage), "metric");
    addBadge(row, agent.alert_status);
    addCommand(row, agent);
    rows.append(row);
  }

  totalCount.textContent = agents.length;
  onlineCount.textContent = agents.filter((agent) => agent.status === "ONLINE").length;
  warningCount.textContent = agents.filter((agent) => agent.alert_status === "WARNING").length;
}

async function restartAgent(agentId, button) {
  button.disabled = true;
  try {
    const response = await fetch(`/api/agents/${agentId}/restart`, { method: "POST" });
    if (!response.ok) throw new Error(`RESTART 요청 실패: HTTP ${response.status}`);
  } catch (error) {
    button.disabled = false;
    connection.textContent = error.message;
    connection.className = "connection error";
    console.error(error);
  }
}

async function refresh() {
  try {
    const response = await fetch("/api/agents", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    render(await response.json());
    connection.textContent = "서버 연결됨";
    connection.className = "connection connected";
    updatedAt.textContent = `최근 갱신 ${new Date().toLocaleTimeString("ko-KR", { hour12: false })}`;
  } catch (error) {
    connection.textContent = "서버 연결 오류";
    connection.className = "connection error";
    console.error(error);
  } finally {
    window.setTimeout(refresh, 1000);
  }
}

refresh();
