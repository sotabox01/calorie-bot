#!/usr/bin/env pwsh
# deploy.ps1 -- run tests, push to git, deploy to VPS

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$venv = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
$remote = "caloriebot-vps"

Write-Host "=== [1/4] Tests ==" -ForegroundColor Cyan
& $venv -m pytest tests/ -q --tb=short
if ($LASTEXITCODE -ne 0) { Write-Host "Tests failed. Deploy cancelled." -ForegroundColor Red; exit 1 }
Write-Host "All tests passed." -ForegroundColor Green

Write-Host "=== [2/4] git push ==" -ForegroundColor Cyan
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Host "git push failed." -ForegroundColor Red; exit 1 }

Write-Host "=== [3/4] DB backup on VPS ==" -ForegroundColor Cyan
ssh $remote "cp ~/calorie-bot/data/calorie_bot.db ~/calorie-bot/data/calorie_bot.db.bak && echo Backup_OK"

Write-Host "=== [4/4] git pull + restart on VPS ==" -ForegroundColor Cyan
$script = Join-Path $PSScriptRoot "deploy_remote.sh"
scp -P 25565 $script "radeonovich@144.31.164.138:/tmp/_deploy.sh"
ssh $remote "bash /tmp/_deploy.sh; rm /tmp/_deploy.sh"

if ($LASTEXITCODE -ne 0) { Write-Host "Deploy failed." -ForegroundColor Red; exit 1 }
Write-Host "Deploy successful." -ForegroundColor Green
