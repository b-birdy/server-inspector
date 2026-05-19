Set-Location -LiteralPath "C:\Users\Administrator\coding-project\server-inspector"
$env:GIT_MASTER = "1"
Write-Host "=== Step 1: git diff --stat ==="
git diff --stat
Write-Host ""
Write-Host "=== Step 2: git add -A ==="
git add -A
Write-Host "Result: files staged"
Write-Host ""
Write-Host "=== Step 3: git commit ==="
git commit -m "fix: switch to SSH protocol for Gitee auth-free pull" -m "Ultraworked with [Sisyphus](https://github.com/code-yeongyu/oh-my-openagent)" -m "Co-authored-by: Sisyphus <clio-agent@sisyphuslabs.ai>"
Write-Host ""
Write-Host "=== Step 4: git push ==="
git push
Write-Host ""
Write-Host "=== Done ==="
git log -1 --oneline
