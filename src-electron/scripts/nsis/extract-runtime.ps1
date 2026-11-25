param(
  [Parameter(Mandatory=$true)]
  [string]$TargetDir
)
$ErrorActionPreference = "Stop"

$dir = Join-Path $TargetDir "resources"
$7z = Join-Path $dir "7z.exe"
$dll = Join-Path $dir "7z.dll"
$log = Join-Path $dir "extract-runtime.log"

function Write-Log($msg) {
  $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $msg
  Add-Content -Path $log -Value $line -ErrorAction SilentlyContinue
  Write-Host $line
}

Write-Log "--- extract-runtime started ---"
Write-Log "TargetDir=$TargetDir"
Write-Log "7z.exe exists=$(Test-Path $7z)"
Write-Log "7z.dll exists=$(Test-Path $dll)"

if (!(Test-Path $7z)) { throw "7z.exe not found at $7z" }
if (!(Test-Path $dll)) { throw "7z.dll not found at $dll" }

# Note: no permanent Defender exclusion is added here. Microsoft/CISA guidance
# recommends against persistent exclusions on user-writable directories.

function Start-7zExtract($archive, $outDir, $extraArgs) {
  $archivePath = Join-Path $dir $archive
  if (!(Test-Path $archivePath)) {
    Write-Log "archive missing, skipping: $archivePath"
    return $null
  }
  $outPath = Join-Path $dir $outDir
  if (!(Test-Path $outPath)) { New-Item -ItemType Directory -Path $outPath -Force | Out-Null }

  # 7z args: -y yes to all; -mmt=8 multi-thread; -bso0 关 stdout；-bse1 错误走 stdout
  # 进度不靠解析 7z 输出：解压期间直接数目标目录里已落盘的文件数（见下面的进度循环）。
  # 原因：OutputDataReceived 的回调跑在另起线程上，PowerShell 的脚本块在那里没有
  # Runspace，会抛 PSInvalidOperationException 把整个进程搞崩（2026-09-22 实测）。
  $baseArgs = @("x", $archivePath, "-o$outPath", "-y", "-mmt=8", "-bso0", "-bse1")
  $allArgs = $baseArgs + $extraArgs
  $argString = ($allArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '

  Write-Log "extracting $archive -> $outPath"
  Write-Log "7z arguments: $argString"

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $7z
  $psi.Arguments = $argString
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true

  $proc = [System.Diagnostics.Process]::Start($psi)

  # Async read stdout/stderr to avoid pipe deadlock on large output
  $stdout = $proc.StandardOutput.ReadToEndAsync()
  $stderr = $proc.StandardError.ReadToEndAsync()

  return @{ Process = $proc; StdOut = $stdout; StdErr = $stderr; Archive = $archive; OutPath = $outPath }
}

$jobs = @()
$jobs += Start-7zExtract "python-core.7z" "python-embed" @("-xr!__pycache__")
$jobs += Start-7zExtract "python-ml.7z" "python-embed" @("-xr!__pycache__")
$jobs += Start-7zExtract "python-chromadb.7z" "python-embed" @("-xr!__pycache__")
  # (node-core / node-openclaw 已移除：OpenClaw/Hermes 改为外部 agent)
  # python-chromadb.7z 已不再随包分发（改用 sqlite-vec）；这一行只为旧版本升级时
  # 清掉历史解压残留——归档不存在时 Start-7zExtract 会跳过。
$jobs = $jobs | Where-Object { $_ -ne $null }

$failed = @()
$total = $jobs.Count
$done = 0
$lastPct = -1

# 所有归档同时解压（互不依赖）——串行等 68+40 秒，并行后总时长由最慢的决定。
# 进度按"已落盘文件数 / 预计文件数"估算：预计值取归档内条目数的 0.9 倍
# （含目录条目，7z 列出的条目数略多于实际落盘文件数），封顶 99% 留给完成时刻。
# 预计文件数：按归档体积估算（~150 文件/MB，只用于进度显示，不参与正确性判断）
$expected = @{}
foreach ($job in $jobs) {
  $sz = (Get-Item (Join-Path $dir $job.Archive)).Length
  $expected[$job.Archive] = [Math]::Max(1, [int]($sz / 1024 / 1024 * 150))
}

while ($done -lt $total) {
  foreach ($job in $jobs) {
    if ($job.Done) { continue }
    $proc = $job.Process
    if ($proc.HasExited) {
      $proc.WaitForExit()
      $outText = $job.StdOut.Result
      $errText = $job.StdErr.Result
      $job.Done = $true
      $done++
      Write-Log "archive=$($job.Archive) exit=$($proc.ExitCode)"
      if ($errText) { Write-Log "stderr:`n$errText" }
      if ($proc.ExitCode -ne 0) {
        $failed += "$($job.Archive): exit code $($proc.ExitCode)"
      }
    }
  }
  if ($done -ge $total) { break }

  # 整体进度：已完成归档算 100%，进行中的按已落盘文件数折算
  $sum = 0.0
  foreach ($job in $jobs) {
    if ($job.Done) { $sum += 100.0; continue }
    $cur = 0
    if ($job.OutPath -and (Test-Path $job.OutPath)) {
      $cur = @(Get-ChildItem $job.OutPath -Recurse -File -ErrorAction SilentlyContinue).Count
    }
    $pct = [double]$cur / [double]$expected[$job.Archive] * 100.0
    if ($pct -gt 99) { $pct = 99 }
    $sum += $pct
  }
  $percent = [int]($sum / $total)
  if ($percent -gt 99) { $percent = 99 }
  if ($percent -ne $lastPct) {
    $lastPct = $percent
    $busy = ($jobs | Where-Object { -not $_.Done } | Select-Object -First 1).Archive
    # 与 main.js _extractRuntime 的正则 /^YUNCAT_EXTRACT_PROGRESS:(d+):(.+)$/ 对齐
    Write-Host "YUNCAT_EXTRACT_PROGRESS:${percent}:$busy"
  }
  Start-Sleep -Milliseconds 800
}

Write-Host "YUNCAT_EXTRACT_PROGRESS:100:done"

Write-Log "--- extract-runtime finished ---"

# Failure handling: keep archives for retry (Docker Desktop / Postman convention)
# If archives are deleted on failure, retry install skips extraction (missing archives),
# causing unrecoverable "install success but runtime not extracted" state
if ($failed.Count -gt 0) {
  Write-Log "extraction failed, archives kept for retry: $($failed -join '; ')"
  throw ($failed -join [Environment]::NewLine)
}

# Clean up archives and tools only after all succeed (save disk space)
Remove-Item "$dir\*.7z" -Force -ErrorAction SilentlyContinue
Remove-Item $7z -Force -ErrorAction SilentlyContinue
Remove-Item $dll -Force -ErrorAction SilentlyContinue
Remove-Item $PSCommandPath -Force -ErrorAction SilentlyContinue
