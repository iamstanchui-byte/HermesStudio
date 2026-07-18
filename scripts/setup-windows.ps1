#!/usr/bin/env pwsh
# Windows setup helper — to be run as Administrator.
# Per REVIEW.md §8.1: register agent as Windows Service via NSSM.

# Usage:
#   .\scripts\setup-windows.ps1 -OrchestratorUrl "http://192.168.1.10:8765" -AgentId "windows-b-01" -Roles "mt5-automation,report-writer"

param(
    [Parameter(Mandatory=$true)][string]$OrchestratorUrl,
    [Parameter(Mandatory=$true)][string]$AgentId,
    [Parameter(Mandatory=$true)][string]$Roles
)

Write-Host "=== Hermes Orchestrator Agent — Windows Setup ===" -ForegroundColor Cyan
Write-Host "Orchestrator: $OrchestratorUrl"
Write-Host "Agent ID:     $AgentId"
Write-Host "Roles:        $Roles"
Write-Host ""

# 1. Install package
Write-Host "[1/4] Installing hermes-orchestrator..." -ForegroundColor Yellow
pip install hermes-orchestrator

# 2. Register with orchestrator (interactive — asks for admin token)
Write-Host ""
Write-Host "[2/4] Registering agent..." -ForegroundColor Yellow
hermes-orch-agent register `
    --orchestrator $OrchestratorUrl `
    --agent-id $AgentId `
    --roles $Roles

# 3. Install as Windows Service via NSSM
Write-Host ""
Write-Host "[3/4] Installing Windows Service (NSSM)..." -ForegroundColor Yellow
$pythonPath = (Get-Command python).Source
$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    Write-Error "NSSM not found. Install: choco install nssm"
    exit 1
}

nssm install "HermesOrchAgent_$AgentId" $pythonPath "-m hermes_orch.agent_cli start"
nssm set "HermesOrchAgent_$AgentId" AppDirectory $PSScriptRoot\..
nssm set "HermesOrchAgent_$AgentId" DisplayName "Hermes Orchestrator Agent ($AgentId)"
nssm set "HermesOrchAgent_$AgentId" Start SERVICE_AUTO_START

# 4. Start service
Write-Host ""
Write-Host "[4/4] Starting service..." -ForegroundColor Yellow
nssm start "HermesOrchAgent_$AgentId"

Write-Host ""
Write-Host "✅ Setup complete. Service: HermesOrchAgent_$AgentId" -ForegroundColor Green
Write-Host "   Check: Get-Service 'HermesOrchAgent_$AgentId'"
Write-Host "   Logs:  Get-EventLog -LogName Application -Source 'HermesOrchAgent_$AgentId' -Newest 20"
