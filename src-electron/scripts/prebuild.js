/**
 * prebuild.js — 运行时预处理
 *
 * Phase 49 原方案：loose files + 独立压缩（NSIS solid:false）
 *   问题：66K 文件独立压缩，打包耗时 ~24 分钟
 *
 * 新方案（对标 Docker Desktop / Postman）：
 *   预压缩 7z 归档 + NSIS 安装时解压。
 *   运行时拆分为 2 个独立 7z 组件，便于：
 *     1. 并行解压（SSD 上显著加速首次安装）
 *     2. 选择性更新（差分更新关闭，但单组件变化只重打包该组件）
 *     3. 降低单次更新下载量（前端/源码变化不影响运行时 7z）
 *
 * 拆分方案：
 *   python-core.7z      python.exe + 标准库 + site-packages（含 sqlite-vec）
 *   python-ml.7z        torch / transformers / scipy / sympy / sklearn / networkx 等
 *
 * 说明：OpenClaw / Hermes 已改为外部 agent（用户本地自行安装/启动网关），
 *   不再随安装包分发 node-embed 运行时。
 *
 * 注意：chromadb 已被 sqlite-vec 替代（~200KB vs ~200MB），不再需要独立 7z 组件。
 *
 * 职责：
 *   1. 修复嵌入式 Python _pth 文件（fixPth）
 *   2. 按需生成运行时 7z 组件（packComponentIfChanged）
 */
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');           // src-electron

// Phase 43 P3.1: 构建时间追踪
const BUILD_START = Date.now();
const BUILD_TIMEOUT_SEC = 120;
const ARCHIVE_TIMEOUT_SEC = 600;
const PYTHON_SRC = path.resolve(ROOT, '..', 'src-python', 'python-embed');
const HASH_FILE = path.resolve(ROOT, '..', 'src-python', '.prebuild-hash');

// ── 7z.exe 探测 ──────────────────────────────────────────
// 优先项目自带的完整版 7z.exe（resources 目录，随安装包分发）
// 避免使用 scoop shim（133KB），shim 会导致子进程管理异常和 decompression 失败
function find7z() {
  const local7z = path.resolve(__dirname, 'nsis', '7z.exe');
  if (fs.existsSync(local7z) && fs.statSync(local7z).size > 500000) return local7z;

  const candidates = [
    'C:\\Program Files\\7-Zip\\7z.exe',
    'C:\\Program Files (x86)\\7-Zip\\7z.exe',
    path.join(process.env.USERPROFILE || '', 'scoop\\apps\\7zip\\current\\7z.exe'),
    process.env.PATH.split(path.delimiter)
      .map(p => path.join(p, '7z.exe'))
      .filter(p => p && !p.toLowerCase().includes('scoop\\shims')),
  ].flat();
  for (const p of candidates) {
    if (p && fs.existsSync(p) && fs.statSync(p).size > 500000) return p;
  }
  return null;
}
const SEVEN_ZIP = find7z();

// ── 组件定义：2 个 7z 的内容归属 ────────────────────────
// python-ml: 本地 ML 推理栈（torch 490MB 主因），sentence-transformers 依赖 torch
// python-core: Python 运行时 + 标准库 + 其他 site-packages（fastapi/uvicorn/pydantic/sqlite-vec 等）
const ML_PACKAGES = [
  'torch', 'torchvision', 'torchaudio',
  'transformers', 'sentence_transformers',
  'tokenizers', 'safetensors', 'huggingface_hub',
  'scipy', 'scikit_learn', 'sklearn',
  'networkx', 'sympy',
  // ML 包的 dist-info 目录（pip 元数据）
  'torch-*.dist-info', 'torchvision-*.dist-info', 'torchaudio-*.dist-info',
  'transformers-*.dist-info', 'sentence_transformers-*.dist-info',
  'tokenizers-*.dist-info', 'safetensors-*.dist-info', 'huggingface_hub-*.dist-info',
  'scipy-*.dist-info', 'scikit_learn-*.dist-info', 'networkx-*.dist-info', 'sympy-*.dist-info',
];

// ── _pth 标准化 ──────────────────────────────────────────
function fixPth() {
  const candidates = fs.readdirSync(PYTHON_SRC).filter(f => f.match(/^python\d+\._pth$/));
  if (candidates.length === 0) {
    console.warn('[_pth] no _pth file found — Python may not initialize correctly');
    return;
  }
  const pthFile = path.join(PYTHON_SRC, candidates[0]);
  const expected = 'python312.zip\n.\nLib/site-packages\nScripts\nimport site\n';
  const current = fs.existsSync(pthFile) ? fs.readFileSync(pthFile, 'utf-8') : '';
  if (current !== expected) {
    fs.writeFileSync(pthFile, expected, 'utf-8');
    console.log('[_pth] fixed');
  }
}

// ── 字节码预编译（Phase 49-H 启动优化） ──────────────────
// 首次 import 时 Python 会编译 .py → .pyc 缓存到 __pycache__/。
// 在打包阶段预编译可避免用户首次启动时重复编译（首次 import 提速 ~20%）。
// 嵌入式 Python 锁定 3.12 版本，.pyc 同版本兼容安全。
function precompileBytecode() {
  const pythonExe = path.join(PYTHON_SRC, 'python.exe');
  if (!fs.existsSync(pythonExe)) {
    console.log('[compileall] embedded python not found — skip');
    return;
  }
  try {
    console.log('[compileall] precompiling .py → .pyc ...');
    execSync(`"${pythonExe}" -m compileall -q -f "${PYTHON_SRC}"`, {
      windowsHide: true,
      maxBuffer: 10 * 1024 * 1024,
      timeout: 120_000,
    });
    console.log('[compileall] done');
  } catch (e) {
    console.warn('[compileall] failed (will fall back to runtime compile):', e.message);
  }
}

// ── 文件树哈希（支持 glob 包名匹配） ─────────────────────
function hashDir(dir) {
  if (!fs.existsSync(dir)) return '';
  const files = [];
  function walk(d) {
    for (const name of fs.readdirSync(d)) {
      const p = path.join(d, name);
      const stat = fs.statSync(p);
      if (stat.isDirectory()) { walk(p); continue; }
      if (name.endsWith('.pyc') || name === '__pycache__') continue;
      const data = fs.readFileSync(p);
      const h = crypto.createHash('sha256').update(data).digest('hex');
      files.push({ rel: path.relative(dir, p), size: stat.size, hash: h });
    }
  }
  walk(dir);
  files.sort((a, b) => a.rel.localeCompare(b.rel));
  const manifest = files.map(f => `${f.rel}|${f.size}|${f.hash}`).join('\n');
  return crypto.createHash('sha256').update(manifest).digest('hex');
}

// 收集 site-packages 中指定包名（支持 glob）的相对路径列表
function collectPackages(sitePackagesDir, packageGlobs) {
  if (!fs.existsSync(sitePackagesDir)) return [];
  const entries = fs.readdirSync(sitePackagesDir);
  const matched = [];
  for (const glob of packageGlobs) {
    // 简单 glob：* 匹配任意字符
    const regex = new RegExp('^' + glob.replace(/\./g, '\\.').replace(/\*/g, '.*') + '$');
    for (const entry of entries) {
      if (regex.test(entry) && !matched.includes(entry)) {
        matched.push(entry);
      }
    }
  }
  return matched.map(e => path.join(sitePackagesDir, e));
}

// 计算给定路径列表的聚合 hash（用于增量构建判断）
function hashPaths(baseDir, paths) {
  const files = [];
  function walk(d) {
    for (const name of fs.readdirSync(d)) {
      const p = path.join(d, name);
      const stat = fs.statSync(p);
      if (stat.isDirectory()) { walk(p); continue; }
      if (name.endsWith('.pyc') || name === '__pycache__') continue;
      const data = fs.readFileSync(p);
      const h = crypto.createHash('sha256').update(data).digest('hex');
      files.push({ rel: path.relative(baseDir, p), size: stat.size, hash: h });
    }
  }
  for (const p of paths) {
    if (fs.existsSync(p)) walk(p);
  }
  files.sort((a, b) => a.rel.localeCompare(b.rel));
  const manifest = files.map(f => `${f.rel}|${f.size}|${f.hash}`).join('\n');
  return crypto.createHash('sha256').update(manifest).digest('hex');
}

// ── 7z 压缩 ──────────────────────────────────────────────
// @listPath: 7z 文件列表路径（@ 前缀），null 则压缩整个 srcDir
function run7z(archivePath, srcDir, listPaths, baseDir) {
  if (!SEVEN_ZIP) throw new Error('7z.exe not found — install 7-Zip or add to PATH');
  // 生成文件列表临时文件
  const listFile = archivePath + '.list';
  const listContent = listPaths.map(p => path.relative(baseDir, p)).join('\r\n');
  fs.writeFileSync(listFile, listContent, 'utf-8');
  try {
    // -t7z: 7z 格式
    // -m0=LZMA2: LZMA2 算法
    // -mx=5: 压缩级别（Phase 49-H: 7→5，打包速度 ~2x，体积 +10% 可接受）
    // -mmt=8: 多线程（8 线程）
    // -xr!__pycache__ -xr!*.pyc: 排除字节码缓存。
    //   理由：extract-runtime.ps1 解压时同样用 -xr!__pycache__ 排除，打包包含 .pyc 是无用功。
    //   更关键：precompileBytecode 生成的 .pyc 被 Windows Defender 实时扫描/隔离，
    //   7z 扫描目录后读取文件时已被删除，导致 "系统找不到指定的文件" → exit 1。
    //   排除后既消除竞态、又减小归档、又加快打包。运行时 Python 自动按需编译。
    const cmd = `"${SEVEN_ZIP}" a -t7z -m0=LZMA2 -mx=5 -mmt=8 -xr!__pycache__ -xr!*.pyc "${archivePath}" @"${listFile}"`;
    execSync(cmd, {
      windowsHide: true,
      maxBuffer: 10 * 1024 * 1024,
      timeout: ARCHIVE_TIMEOUT_SEC * 1000,
      cwd: baseDir,
    });
  } finally {
    if (fs.existsSync(listFile)) fs.unlinkSync(listFile);
  }
}

// 压缩整个目录到 7z（python-core / python-ml）
function run7zDir(archivePath, srcDir) {
  if (!SEVEN_ZIP) throw new Error('7z.exe not found — install 7-Zip or add to PATH');
  // Phase 49-H: 与 run7z 对齐——mx=5（速度 ~2x，体积 +10%），保留 __pycache__/.pyc
  // （预编译字节码随包分发，首次 import 提速 ~20%）。node-* 组件无 .pyc，但保持参数一致便于维护。
  const cmd = `"${SEVEN_ZIP}" a -t7z -m0=LZMA2 -mx=5 -mmt=8 "${archivePath}" "${srcDir}\\*"`;
  execSync(cmd, {
    windowsHide: true,
    maxBuffer: 10 * 1024 * 1024,
    timeout: ARCHIVE_TIMEOUT_SEC * 1000,
  });
}

// ── 增量压缩组件（带 hash 比对） ─────────────────────────
function packComponentIfChanged(label, archivePath, newHash, oldHash, packFn) {
  // 归档本身必须在场：hash 只说明"源码没变"，不代表"上次打的包还在"。
  // 这两个 7z 不进版本管理（python-embed/ 被忽略），换机器或清理过目录后
  // 缓存里的 hash 仍然匹配，只比 hash 就会静默跳过、打包时才缺文件。
  const archiveExists = fs.existsSync(archivePath);
  if (newHash === oldHash && archiveExists) {
    console.log(`[${label}] unchanged — skip`);
    return { rebuilt: false, hash: oldHash };
  }
  if (!newHash) return { rebuilt: false, hash: oldHash };
  if (newHash === oldHash) {
    console.log(`[${label}] hash unchanged but archive missing — rebuilding`);
  }
  console.log(`[${label}] changed — rebuilding 7z...`);
  if (fs.existsSync(archivePath)) fs.unlinkSync(archivePath);
  packFn();
  console.log(`[${label}] done`);
  return { rebuilt: true, hash: newHash };
}

// ── 预装 Hermes Agent pip 包到嵌入式 Python ─────────────────
// (installHaPipDeps 已移除：Hermes 改为外部 agent，Yuncat 不再 vendored 其 subprocess)

// ── mtime 快速通道：避免遍历 ~1GB 文件计算 SHA256 ────────
// 记录上次构建时间戳，如果源目录最新 mtime 早于上次构建时间，直接跳过所有 hash 计算。
// 首次构建或无缓存时回退到全量 hash。
function getLatestMtime(dir) {
  if (!fs.existsSync(dir)) return 0;
  let latest = 0;
  function walk(d) {
    for (const name of fs.readdirSync(d)) {
      // 跳过 __pycache__ / .pyc——与 hash 函数对齐。
      // 否则 precompileBytecode 或运行时自动编译会更新 .pyc mtime，
      // 导致 mtime fast-path 失效、触发 ~20 分钟的全量 hash。
      if (name === '__pycache__' || name.endsWith('.pyc')) continue;
      const p = path.join(d, name);
      try {
        const stat = fs.statSync(p);
        const mt = stat.mtimeMs;
        if (mt > latest) latest = mt;
        if (stat.isDirectory()) walk(p);
      } catch { /* skip broken symlinks etc. */ }
    }
  }
  walk(dir);
  return latest;
}

// ── Main ───────────────────────────────────────────────────
fixPth();
// precompileBytecode 仅在 loose files 模式下有意义（.pyc 直接随文件分发）。
// 7z 模式下 .pyc 被 -xr!__pycache__ 排除出归档，预编译纯属浪费时间
// 且 .pyc mtime 污染会导致 mtime fast-path 失效、触发全量 hash。
const SKIP_ARCHIVE = process.env.YUNCAT_SKIP_ZIP === '1';
if (SKIP_ARCHIVE) precompileBytecode();

if (SKIP_ARCHIVE) {
  console.log('[prebuild] YUNCAT_SKIP_ZIP=1 — skip 7z generation (loose files mode)');
} else if (!SEVEN_ZIP) {
  console.warn('[prebuild] 7z.exe not found — skip 7z generation');
  console.warn('[prebuild] Install 7-Zip or add 7z.exe to PATH');
} else {
  console.log(`[prebuild] using 7z: ${SEVEN_ZIP}`);
  const cacheData = (() => {
    try { return JSON.parse(fs.readFileSync(HASH_FILE, 'utf-8')); } catch { return {}; }
  })();
  const oldHashes = { ...cacheData };
  delete oldHashes._buildTime;

  const archiveDir = path.resolve(ROOT, '..', 'src-python');

  // mtime 快速通道：比较源目录最新 mtime 与上次构建时间
  const pyMtime = getLatestMtime(PYTHON_SRC);
  const latestMtime = pyMtime;
  const lastBuildTime = cacheData._buildTime || 0;
  const all7zExist = ['python-core', 'python-ml']
    .every(name => fs.existsSync(path.join(archiveDir, `${name}.7z`)));

  // all7zExist 已保证两份归档都在场；这里再比 mtime，避免"源码没动但包不在"被跳过
  if (all7zExist && lastBuildTime > 0 && latestMtime <= lastBuildTime) {
    console.log('[prebuild] mtime fast-path: no source changes since last build — all components skip');
    console.log(`[prebuild] latest mtime: ${new Date(latestMtime).toISOString()}, last build: ${new Date(lastBuildTime).toISOString()}`);
  } else {
    if (lastBuildTime > 0 && latestMtime <= lastBuildTime && !all7zExist) {
      console.log('[prebuild] mtime unchanged but some .7z missing — rebuilding missing components');
    } else if (lastBuildTime > 0) {
      console.log('[prebuild] source changes detected — recomputing hashes');
    } else {
      console.log('[prebuild] no cache found — full hash computation');
    }

    const sitePackagesDir = path.join(PYTHON_SRC, 'Lib', 'site-packages');
    const results = {};

    // ── Component 1: python-core ──
    const mlPaths = collectPackages(sitePackagesDir, ML_PACKAGES);
    const excludedPaths = new Set([...mlPaths].map(p => path.resolve(p)));

    function hashPythonCore() {
      const files = [];
      function walk(d) {
        for (const name of fs.readdirSync(d)) {
          const p = path.join(d, name);
          if (excludedPaths.has(path.resolve(p))) continue;
          const stat = fs.statSync(p);
          if (stat.isDirectory()) { walk(p); continue; }
          if (name.endsWith('.pyc') || name === '__pycache__') continue;
          const data = fs.readFileSync(p);
          const h = crypto.createHash('sha256').update(data).digest('hex');
          files.push({ rel: path.relative(PYTHON_SRC, p), size: stat.size, hash: h });
        }
      }
      walk(PYTHON_SRC);
      files.sort((a, b) => a.rel.localeCompare(b.rel));
      const manifest = files.map(f => `${f.rel}|${f.size}|${f.hash}`).join('\n');
      return crypto.createHash('sha256').update(manifest).digest('hex');
    }

    function buildPythonCoreList() {
      const list = [];
      function walk(d) {
        for (const name of fs.readdirSync(d)) {
          const p = path.join(d, name);
          if (excludedPaths.has(path.resolve(p))) continue;
          const stat = fs.statSync(p);
          if (stat.isDirectory()) { walk(p); }
          else {
            if (name.endsWith('.pyc') || name === '__pycache__') continue;
            list.push(p);
          }
        }
      }
      walk(PYTHON_SRC);
      return list;
    }

    const coreHash = hashPythonCore();
    results.python_core = packComponentIfChanged(
      'python-core',
      path.join(archiveDir, 'python-core.7z'),
      coreHash, oldHashes.python_core || '',
      () => run7z(path.join(archiveDir, 'python-core.7z'), PYTHON_SRC, buildPythonCoreList(), PYTHON_SRC),
    );

    // ── Component 2: python-ml ──
    const mlHash = hashPaths(PYTHON_SRC, mlPaths);
    results.python_ml = packComponentIfChanged(
      'python-ml',
      path.join(archiveDir, 'python-ml.7z'),
      mlHash, oldHashes.python_ml || '',
      () => run7z(path.join(archiveDir, 'python-ml.7z'), PYTHON_SRC, mlPaths, PYTHON_SRC),
    );

    // 写入 hash 文件（含构建时间戳）
    const newCache = { _buildTime: Date.now() };
    for (const [k, v] of Object.entries(results)) newCache[k] = v.hash;
    fs.writeFileSync(HASH_FILE, JSON.stringify(newCache, null, 2), 'utf-8');

    const rebuilt = Object.values(results).some(r => r.rebuilt);
    console.log(rebuilt ? 'prebuild: complete (some components rebuilt)' : 'prebuild: all components up-to-date');
  }
}

// 生成安装时并行解压脚本
function writeExtractScript() {
  const scriptDir = path.resolve(ROOT, 'scripts', 'nsis');
  if (!fs.existsSync(scriptDir)) fs.mkdirSync(scriptDir, { recursive: true });
  const scriptPath = path.join(scriptDir, 'extract-runtime.ps1');
  const script = `param(
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
      if ($errText) { Write-Log "stderr:\`n$errText" }
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
    # 与 main.js _extractRuntime 的正则 /^YUNCAT_EXTRACT_PROGRESS:(\d+):(.+)$/ 对齐
    Write-Host "YUNCAT_EXTRACT_PROGRESS:\${percent}:$busy"
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
Remove-Item "$dir\\*.7z" -Force -ErrorAction SilentlyContinue
Remove-Item $7z -Force -ErrorAction SilentlyContinue
Remove-Item $dll -Force -ErrorAction SilentlyContinue
Remove-Item $PSCommandPath -Force -ErrorAction SilentlyContinue
`;
  // Write with UTF-8 BOM: PowerShell 5.x (Windows 11 China default) parses BOM-less
  // UTF-8 .ps1 files as ANSI/GBK, breaking string parsing when non-ASCII chars are
  // present (e.g. Chinese comments). UTF-8 BOM (EF BB BF) forces PowerShell 5.x
  // to use UTF-8 decoding. See: https://github.com/PowerShell/PowerShell/issues/3753
  // Even though the template is now pure ASCII, keep BOM as defense-in-depth for
  // future edits that may reintroduce non-ASCII chars.
  const BOM = '\uFEFF';
  fs.writeFileSync(scriptPath, BOM + script, 'utf-8');
  console.log('[prebuild] generated extract-runtime.ps1 (UTF-8 with BOM)');
}

writeExtractScript();

// Phase 43 P3.1: 构建时间报告
const elapsed = ((Date.now() - BUILD_START) / 1000).toFixed(1);
console.log(`[prebuild] Done in ${elapsed}s`);
if (parseFloat(elapsed) > BUILD_TIMEOUT_SEC) {
  console.warn(`[prebuild] WARNING: Build took ${elapsed}s (exceeded ${BUILD_TIMEOUT_SEC}s threshold)`);
}
