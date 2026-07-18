# test-artifacts.ps1 - Test artifact endpoints (REVIEW.md §5)

function J {
    param([Parameter(ValueFromPipeline=$true)] $o)
    process { $o | ConvertTo-Json -Depth 5 }
}

$BASE = "http://localhost:8765"

Write-Host "=== Hermes Orchestrator artifact API test ===" -ForegroundColor Cyan

# 0. Set up: create a project + task + agent (if not exists)
Write-Host "`n[0] Setup: ensure project + task + agent exist..." -ForegroundColor Yellow

# Create project
$body = '{"goal":"Artifact test","name":"ArtifactTest"}'
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/projects/" -ContentType "application/json" -Body $body
$PROJ_ID = $r.id
Write-Host "  Project: $PROJ_ID" -ForegroundColor Green

# Get existing demo-agent or register new
try {
    $r = Invoke-RestMethod -Uri "$BASE/api/agents/demo-agent"
    $AGENT_ID = "demo-agent"
    $PROFILE_ID = ($r.profiles | Where-Object { $_.name -eq "demo-runner" }).id
    Write-Host "  Agent: $AGENT_ID (existing)" -ForegroundColor Green
} catch {
    $body = '{"agent_id":"test-agent","roles":["test-runner"]}'
    $r = Invoke-RestMethod -Method Post -Uri "$BASE/api/agents/" -ContentType "application/json" -Body $body
    $AGENT_ID = $r.agent.id
    $PROFILE_ID = ($r.agent.profiles | Select-Object -First 1).id
    Write-Host "  Agent: $AGENT_ID (new)" -ForegroundColor Green
}

# Create task
$body = "{`"project_id`":`"$PROJ_ID`",`"agent_role`":`"test-runner`",`"action`":`"x`"}"
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/tasks/" -ContentType "application/json" -Body $body
$TASK_ID = $r.id
Write-Host "  Task: $TASK_ID" -ForegroundColor Green

# 1. Create a test file and upload it
Write-Host "`n[1] Upload a small file (CSV)..." -ForegroundColor Yellow
$tmpFile = Join-Path $env:TEMP "hermes-test.csv"
"symbol,price,volume`nEURUSD,1.0850,100`nGBPUSD,1.2650,50" | Set-Content -Path $tmpFile
$fileInfo = Get-Item $tmpFile
Write-Host "  Local file: $($fileInfo.FullName) ($($fileInfo.Length) bytes)" -ForegroundColor Green

$form = @{
    file = $fileInfo
    task_id = $TASK_ID
    project_id = $PROJ_ID
}

# Use Python helper for multipart upload (PowerShell 5.x lacks -Form)
$pythonExe = "C:\Project\minimax code\hermes-orchestrator\.venv\Scripts\python.exe"
$helperScript = "C:\Project\minimax code\hermes-orchestrator\scripts\upload-artifact.py"
$ARTIFACT_ID = & $pythonExe $helperScript --file $tmpFile --task-id $TASK_ID --project-id $PROJ_ID
if ($LASTEXITCODE -ne 0) {
    Write-Host "Upload failed" -ForegroundColor Red
    exit 1
}
$r = Invoke-RestMethod -Uri "$BASE/api/artifacts/$ARTIFACT_ID"
J $r
Write-Host "  Artifact ID: $ARTIFACT_ID" -ForegroundColor Green

# 2. Get artifact metadata
Write-Host "`n[2] Get artifact metadata..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "$BASE/api/artifacts/$ARTIFACT_ID" | J

# 3. List artifacts for the task
Write-Host "`n[3] List artifacts for task..." -ForegroundColor Yellow
$r = Invoke-RestMethod -Uri "$BASE/api/artifacts/?task_id=$TASK_ID"
Write-Host "  Count: $($r.artifacts.Count)" -ForegroundColor Green
J $r.artifacts

# 4. Download the file (central) and verify content
Write-Host "`n[4] Download artifact (verify content)..." -ForegroundColor Yellow
$downloadPath = Join-Path $env:TEMP "hermes-download.csv"
Invoke-WebRequest -Method Get -Uri "$BASE/api/artifacts/$ARTIFACT_ID/download" -OutFile $downloadPath
$content = Get-Content $downloadPath -Raw
$expected = "symbol,price,volume`nEURUSD,1.0850,100`nGBPUSD,1.2650,50"
if ($content -eq $expected) {
    Write-Host "  Content matches! OK" -ForegroundColor Green
} else {
    Write-Host "  Content MISMATCH" -ForegroundColor Red
    Write-Host "  Got: $content" -ForegroundColor Red
}

# 5. Register external artifact
Write-Host "`n[5] Register external artifact (large file on agent)..." -ForegroundColor Yellow
$body = @"
{
  "task_id": "$TASK_ID",
  "project_id": "$PROJ_ID",
  "name": "model.pkl",
  "path": "/data/models/run-001/model.pkl",
  "size_bytes": 209715200,
  "agent_id": "$AGENT_ID",
  "checksum": "abc123def456"
}
"@
$r = Invoke-RestMethod -Method Post -Uri "$BASE/api/artifacts/external" -ContentType "application/json" -Body $body
J $r
$EXT_ARTIFACT_ID = $r.id
Write-Host "  External artifact ID: $EXT_ARTIFACT_ID" -ForegroundColor Green

# 6. List artifacts again (should show 2)
Write-Host "`n[6] List artifacts for task (should show 2)..." -ForegroundColor Yellow
$r = Invoke-RestMethod -Uri "$BASE/api/artifacts/?task_id=$TASK_ID"
Write-Host "  Count: $($r.artifacts.Count) (expect 2)" -ForegroundColor Green
J $r.artifacts

# 7. Try to download external (should 501 with scp command)
Write-Host "`n[7] Download external artifact (should 501 with scp command)..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Uri "$BASE/api/artifacts/$EXT_ARTIFACT_ID/download" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    $code = $_.Exception.Response.StatusCode
    Write-Host "  Got: $code  (expect 501)" -ForegroundColor Yellow
    # Try to get the body for the scp command
    $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
    $body = $reader.ReadToEnd()
    Write-Host "  Body: $body" -ForegroundColor Yellow
}

# 8. Get nonexistent artifact (should 404)
Write-Host "`n[8] Get nonexistent artifact (should 404)..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Uri "$BASE/api/artifacts/a-nonexistent" -ErrorAction Stop
    Write-Host "UNEXPECTED: no error" -ForegroundColor Red
} catch {
    Write-Host "  Got: $($_.Exception.Response.StatusCode)  (expect 404)" -ForegroundColor Yellow
}

# 9. Delete the central artifact
Write-Host "`n[9] Delete central artifact..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Method Delete -Uri "$BASE/api/artifacts/$ARTIFACT_ID" -ErrorAction Stop
    Write-Host "  OK (status 204)" -ForegroundColor Green
} catch {
    Write-Host "  Status: $($_.Exception.Response.StatusCode)" -ForegroundColor Yellow
}

# 10. Verify file is gone from disk
Write-Host "`n[10] Verify file removed from disk..." -ForegroundColor Yellow
$storage = (Invoke-RestMethod -Uri "$BASE/api/artifacts/?task_id=$TASK_ID").artifacts[0].storage_path
# We just deleted ARTIFACT_ID, so check the storage path
if (Test-Path $storage) {
    Write-Host "  File still on disk!" -ForegroundColor Red
} else {
    Write-Host "  File removed from disk OK" -ForegroundColor Green
}

# 11. Delete external (only DB record, no file on disk)
Write-Host "`n[11] Delete external artifact (DB only)..." -ForegroundColor Yellow
try {
    Invoke-RestMethod -Method Delete -Uri "$BASE/api/artifacts/$EXT_ARTIFACT_ID" -ErrorAction Stop
    Write-Host "  OK (status 204)" -ForegroundColor Green
} catch {
    Write-Host "  Status: $($_.Exception.Response.StatusCode)" -ForegroundColor Yellow
}

# Cleanup
Remove-Item $tmpFile -ErrorAction SilentlyContinue
Remove-Item $downloadPath -ErrorAction SilentlyContinue

Write-Host "`n=== Artifact API tests done ===" -ForegroundColor Cyan
