<#
  Restart Manager killer for Yuncat installer.

  Uses the Windows Restart Manager API (Rstrtmgr.dll) to ask Windows to shut
  down any application that currently holds a handle on the given file(s).
  This is the same mechanism used by VS Code, Chrome and Windows Update to
  replace in-use binaries without a reboot.

  Must run with enough privileges to terminate the locking process.
  Non-fatal: if RM fails for any reason the script exits 0 so the installer
  can fall back to its retry loop / user prompt.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TargetPath
)

$ErrorActionPreference = 'Stop'

# Skip if the target does not exist (fresh install).
if (-not (Test-Path -LiteralPath $TargetPath)) {
    exit 0
}

# P/Invoke declarations for Restart Manager.
$code = @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public class YuncatRestartManager
{
    [DllImport("Rstrtmgr.dll", CharSet = CharSet.Unicode)]
    public static extern int RmStartSession(out uint pSessionHandle, int dwSessionFlags, StringBuilder strSessionKey);

    [DllImport("Rstrtmgr.dll", CharSet = CharSet.Unicode)]
    public static extern int RmRegisterResources(uint dwSessionHandle, uint nFiles, string[] rgsFilenames,
        uint nApplications, IntPtr rgApplications, uint nServices, IntPtr rgsServices);

    [DllImport("Rstrtmgr.dll")]
    public static extern int RmShutdown(uint dwSessionHandle, int lActionFlags, IntPtr fnStatusCallback);

    [DllImport("Rstrtmgr.dll")]
    public static extern int RmRestart(uint dwSessionHandle, int dwRestartFlags, IntPtr fnStatusCallback);

    [DllImport("Rstrtmgr.dll")]
    public static extern int RmEndSession(uint dwSessionHandle);

    public const int ERROR_MORE_DATA = 234;
}
'@

try {
    Add-Type -TypeDefinition $code -Language CSharp -ErrorAction Stop
} catch {
    # Older .NET or constrained environments may fail to compile; exit silently.
    exit 0
}

function Invoke-RestartManagerKill {
    param([string]$Path)

    $fullPath = (Resolve-Path -LiteralPath $Path).ProviderPath
    $sessionKey = New-Object System.Text.StringBuilder(256)
    [void]$sessionKey.Append([guid]::NewGuid().ToString())

    $hSession = 0
    $hr = [YuncatRestartManager]::RmStartSession([ref]$hSession, 0, $sessionKey)
    if ($hr -ne 0) {
        Write-Verbose "RmStartSession failed: 0x$($hr.ToString('X8'))"
        return
    }

    try {
        $files = @($fullPath)
        $hr = [YuncatRestartManager]::RmRegisterResources($hSession, [uint32]$files.Length, $files, 0, [IntPtr]::Zero, 0, [IntPtr]::Zero)
        if ($hr -ne 0) {
            Write-Verbose "RmRegisterResources failed: 0x$($hr.ToString('X8'))"
            return
        }

        # RmShutdown asks Windows to shut down the applications that are using
        # the registered resources. Windows will try graceful shutdown first and
        # can fall back to forced termination depending on the action flags.
        $hr = [YuncatRestartManager]::RmShutdown($hSession, 0, [IntPtr]::Zero)
        Write-Verbose "RmShutdown returned: 0x$($hr.ToString('X8'))"

        # We intentionally do NOT call RmRestart here; the installer is replacing
        # the binary, so the old application should not be restarted.
    } finally {
        [void][YuncatRestartManager]::RmEndSession($hSession)
    }
}

try {
    Invoke-RestartManagerKill -Path $TargetPath
} catch {
    Write-Verbose "Restart Manager kill failed: $_"
}

exit 0
