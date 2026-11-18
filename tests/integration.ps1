$ErrorActionPreference = "Stop"

$settings = @{}
if (Test-Path .env) {
    foreach ($line in Get-Content .env) {
        if ($line -match '^([^#=]+)=(.+)$') {
            $settings[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
}
foreach ($name in "MQTT_SERVER_PASSWORD", "AGENT_A_PASSWORD", "AGENT_B_PASSWORD", "AGENT_C_PASSWORD", "AGENT_D_PASSWORD", "AGENT_E_PASSWORD") {
    if (-not $settings[$name]) { $settings[$name] = [Environment]::GetEnvironmentVariable($name) }
    if (-not $settings[$name]) { throw "Set $name in .env or the environment before this test." }
}

function Wait-Until([scriptblock]$Condition, [string]$Failure, [int]$Seconds = 45) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            if (& $Condition) { return }
        } catch {}
        Start-Sleep -Seconds 1
    }
    throw $Failure
}

function Get-JsonArray([string]$Uri) {
    $content = (Invoke-WebRequest -UseBasicParsing $Uri).Content
    return @($content | ConvertFrom-Json | ForEach-Object { $_ })
}

docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

Wait-Until {
    $agents = @(Get-JsonArray "http://localhost:8000/api/agents")
    $agents.Count -eq 5 -and @($agents | Where-Object status -eq "ONLINE").Count -eq 5
} "Five agents did not become ONLINE"

docker compose exec -T broker mosquitto_pub -h localhost -u agent-a -P wrong-password -t agents/agent-a/heartbeat -m '{}'
if ($LASTEXITCODE -eq 0) { throw "Invalid MQTT password was accepted" }

$aclOutput = & {
    $ErrorActionPreference = "Continue"
    docker compose exec -T broker mosquitto_pub -V mqttv5 -h localhost -u agent-a -P $settings.AGENT_A_PASSWORD -t agents/agent-b/heartbeat -q 1 -m '{}' 2>&1
} | Out-String
if ($aclOutput -notmatch "Not authorized") { throw "Agent A publish was not explicitly denied: $aclOutput" }

$requestId = [guid]::NewGuid().ToString()
$command = @{ request_id = $requestId; command = "GET_CONFIG"; payload = @{}; status = "PENDING"; requested_at = (Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json -Compress
1..2 | ForEach-Object {
    $command | docker compose exec -T broker mosquitto_pub -h localhost -u control-server -P $settings.MQTT_SERVER_PASSWORD -t agents/agent-a/commands -q 1 -s
    if ($LASTEXITCODE -ne 0) { throw "Duplicate command publish failed" }
}
Start-Sleep -Seconds 2
$count = docker compose exec -T agent-a python -c "import sqlite3; print(sqlite3.connect('/data/agent.db').execute('select count(*) from processed_commands where request_id=?', ('$requestId',)).fetchone()[0])"
if ([int]$count -ne 1) { throw "Duplicate Request ID was stored more than once" }

$created = Invoke-RestMethod -Method Post -ContentType "application/json" -Body '{"command":"GET_SYSTEM_INFO","payload":{}}' http://localhost:8000/api/agents/agent-a/commands
Wait-Until {
    $history = @(Get-JsonArray "http://localhost:8000/api/commands?agent_id=agent-a")
    ($history | Where-Object request_id -eq $created.request_id).status -eq "SUCCESS"
} "GET_SYSTEM_INFO did not complete"

docker compose restart server
Wait-Until {
    $history = @(Get-JsonArray "http://localhost:8000/api/commands?agent_id=agent-a")
    @($history | Where-Object request_id -eq $created.request_id).Count -eq 1
} "Command history did not survive server restart"

$before = (Get-JsonArray "http://localhost:8000/api/agents" | Where-Object agent_id -eq "agent-a").last_heartbeat
docker compose restart broker
Wait-Until {
    $agent = Get-JsonArray "http://localhost:8000/api/agents" | Where-Object agent_id -eq "agent-a"
    $agent.status -eq "ONLINE" -and $agent.last_heartbeat -ne $before
} "Agent did not reconnect and resume heartbeat after broker restart"

Write-Host "PASS: 5 agents, authentication, ACL, duplicate prevention, persistence, and broker recovery"
