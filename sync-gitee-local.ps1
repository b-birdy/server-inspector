# sync-gitee-local.ps1 - 一键替换 GitHub 链接并推送到 Gitee
# 用法：右键 → 使用 PowerShell 运行

$ErrorActionPreference = "Stop"

# 配置
$giteeRepo = "https://gitee.com/wzxdcyy/server-inspector.git"
$localDir = "$env:TEMP\server-inspector-gitee"

Write-Host "=== Gitee 链接替换工具 ===" -ForegroundColor Cyan

# 清理旧目录
if (Test-Path $localDir) {
    Remove-Item -Recurse -Force $localDir
}

# 克隆 Gitee 仓库
Write-Host "[1/4] 正在克隆 Gitee 仓库..." -ForegroundColor Yellow
git clone $giteeRepo $localDir
if ($LASTEXITCODE -ne 0) {
    Write-Error "克隆失败，请检查网络或 Gitee 仓库权限"
}

Set-Location $localDir

# 执行替换脚本
Write-Host "[2/4] 正在替换 GitHub → Gitee 链接..." -ForegroundColor Yellow
bash sync-to-gitee.sh
if ($LASTEXITCODE -ne 0) {
    Write-Error "替换脚本执行失败"
}

# 检查是否有变更
$diff = git diff --name-only
if (-not $diff) {
    Write-Host "没有需要替换的内容（可能已经替换过了）" -ForegroundColor Green
    exit 0
}

Write-Host "变更文件：" -ForegroundColor Gray
$diff | ForEach-Object { Write-Host "  - $_" -ForegroundColor Gray }

# 提交推送
Write-Host "[3/4] 正在提交..." -ForegroundColor Yellow
git add .
git commit -m "sync: replace github links with gitee for distribution"

Write-Host "[4/4] 正在推送到 Gitee..." -ForegroundColor Yellow
git push origin master
if ($LASTEXITCODE -ne 0) {
    Write-Error "推送失败，可能需要输入 Gitee 用户名密码"
}

Write-Host "" 
Write-Host "完成！Gitee 仓库已更新。" -ForegroundColor Green
Write-Host "Gitee 地址: https://gitee.com/wzxdcyy/server-inspector" -ForegroundColor Cyan

# 清理
Set-Location $env:TEMP
Remove-Item -Recurse -Force $localDir -ErrorAction SilentlyContinue