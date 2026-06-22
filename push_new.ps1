Set-Location "D:\Claude Projects\INVEST Stocks\Invest Stocks Portfolio"

Write-Host "=== Fetching remote HEAD ===" -ForegroundColor Cyan
git fetch origin
$remoteHead = git rev-parse origin/main
Write-Host "Remote HEAD: $remoteHead" -ForegroundColor Green

Write-Host "=== Building new commit ===" -ForegroundColor Cyan
$tmpIdx = "$env:TEMP\git_idx_push_$([System.Diagnostics.Process]::GetCurrentProcess().Id)"

$env:GIT_INDEX_FILE = $tmpIdx
git read-tree $remoteHead

# Core Python scripts
git add tv_enrich.py
git add finviz_scan.py
git add track_new_tickers.py

# GitHub Actions workflow
git add .github/workflows/update-dashboard.yml

# This push script itself
git add push_new.ps1

# prev_tickers history (if present)
if (Test-Path "prev_tickers.json") { git add prev_tickers.json }

$tree = git write-tree
Write-Host "Tree: $tree" -ForegroundColor Green
Remove-Item $tmpIdx -ErrorAction SilentlyContinue
Remove-Item Env:\GIT_INDEX_FILE -ErrorAction SilentlyContinue

$msg = Read-Host "Commit message (Enter = 'fix: workflow robustness + push all files')"
if ([string]::IsNullOrWhiteSpace($msg)) {
    $msg = "fix: workflow robustness + push all files"
}

$commit = git commit-tree $tree -p $remoteHead -m $msg
Write-Host "Commit: $commit" -ForegroundColor Green

Write-Host "=== Pushing ===" -ForegroundColor Cyan
git push origin "${commit}:refs/heads/main"

Write-Host "=== Done ===" -ForegroundColor Green
Read-Host "Press Enter to exit"
