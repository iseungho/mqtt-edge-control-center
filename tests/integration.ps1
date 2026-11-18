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

function Assert-SystemInfo([string]$AgentId) {
    $created = Invoke-RestMethod -Method Post -ContentType "application/json" -Body '{"command":"GET_SYSTEM_INFO","payload":{}}' "http://localhost:8000/api/agents/$AgentId/commands"
    Wait-Until {
        $history = @(Get-JsonArray "http://localhost:8000/api/commands?agent_id=$AgentId")
        ($history | Where-Object request_id -eq $created.request_id).status -eq "SUCCESS"
    } "GET_SYSTEM_INFO did not complete for $AgentId"
    return $created
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

# Test message delivery because a SUBACK alone does not establish read access.
$subscriber = Start-Job -ArgumentList (Get-Location).Path -ScriptBlock {
    param($ProjectPath)
    Set-Location -LiteralPath $ProjectPath
    @'
import os
from threading import Event

from paho.mqtt import client as mqtt

subscribed = Event()
client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.username_pw_set(os.environ["MQTT_USERNAME"], os.environ["MQTT_PASSWORD"])
client.on_connect = lambda client, *_: client.subscribe([
    ("agents/agent-a/commands", 1),
    ("agents/agent-b/commands", 1),
])

def on_subscribe(*_):
    print("SUBACK", flush=True)
    subscribed.set()

client.on_subscribe = on_subscribe
client.on_message = lambda _client, _userdata, message: print(message.topic, flush=True)
client.connect(os.environ["MQTT_HOST"], int(os.getenv("MQTT_PORT", "1883")))
client.loop_start()
try:
    if not subscribed.wait(10):
        raise SystemExit("MQTT subscription did not complete")
    Event().wait(15)
finally:
    client.disconnect()
    client.loop_stop()
'@ | docker compose exec -T agent-a python -u -
    if ($LASTEXITCODE -ne 0) { throw "ACL subscriber failed" }
}
try {
    Wait-Until {
        @(Receive-Job -Job $subscriber -Keep) -contains "SUBACK"
    } "Agent A test subscriber did not receive SUBACK" 15
    $null = Assert-SystemInfo "agent-b"
    $created = Assert-SystemInfo "agent-a"
    if (-not (Wait-Job -Job $subscriber -Timeout 25)) { throw "ACL subscriber did not finish" }
    $received = @(Receive-Job -Job $subscriber -Keep)
    if ($received -contains 'agents/agent-b/commands') {
        throw "Agent A received a command for Agent B"
    }
    if ($received -notcontains 'agents/agent-a/commands') { throw "Agent A did not receive its own command" }
} finally {
    if ($subscriber.State -eq "Running") { Stop-Job -Job $subscriber }
    Remove-Job -Job $subscriber -Force
}

$requestId = [guid]::NewGuid().ToString()
$command = @{ request_id = $requestId; command = "GET_CONFIG"; payload = @{}; status = "PENDING"; requested_at = (Get-Date).ToUniversalTime().ToString("o") } | ConvertTo-Json -Compress
1..2 | ForEach-Object {
    $command | docker compose exec -T broker mosquitto_pub -h localhost -u control-server -P $settings.MQTT_SERVER_PASSWORD -t agents/agent-a/commands -q 1 -s
    if ($LASTEXITCODE -ne 0) { throw "Duplicate command publish failed" }
}
Start-Sleep -Seconds 2
$count = docker compose exec -T agent-a python -c "import sqlite3; print(sqlite3.connect('/data/agent.db').execute('select count(*) from processed_commands where request_id=?', ('$requestId',)).fetchone()[0])"
if ([int]$count -ne 1) { throw "Duplicate Request ID was stored more than once" }

docker compose restart server
if ($LASTEXITCODE -ne 0) { throw "Server restart failed" }
Wait-Until {
    $history = @(Get-JsonArray "http://localhost:8000/api/commands?agent_id=agent-a")
    @($history | Where-Object request_id -eq $created.request_id).Count -eq 1
} "Command history did not survive server restart"

$before = (Get-JsonArray "http://localhost:8000/api/agents" | Where-Object agent_id -eq "agent-a").last_heartbeat
docker compose restart broker
if ($LASTEXITCODE -ne 0) { throw "Broker restart failed" }
Wait-Until {
    $agent = Get-JsonArray "http://localhost:8000/api/agents" | Where-Object agent_id -eq "agent-a"
    $agent.status -eq "ONLINE" -and $agent.last_heartbeat -ne $before
} "Agent did not reconnect and resume heartbeat after broker restart"
$null = Assert-SystemInfo "agent-a"

Write-Host "PASS: 5 agents, authentication, ACL, duplicate prevention, persistence, and broker recovery"
