; custom.nsh - electron-builder NSIS custom script (single-file entry point)
;
; electron-builder 25.x nsis.include only supports a single string, not an array.
; This file aggregates customInit / customInstall / customUnInstall macros.

!include "LogicLib.nsh"
!include "WinMessages.nsh"

; Request highest available execution level: needed to terminate Yuncat
; when it is running with elevated privileges.
; "highest" auto-elevates via UAC for admins; standard users still install.
RequestExecutionLevel highest

; -- customInit: terminate any running Yuncat on installer init --
; Runs early so the app is closed before the user reaches the welcome page.
; CHECK_APP_RUNNING (patched) will call FORCE_KILL_YUNCAT again later;
; the second call is idempotent because taskkill returns 128 when no process exists.
!macro customInit
  DetailPrint "Closing running Yuncat..."
  !insertmacro FORCE_KILL_YUNCAT
!macroend

; -- customCheckAppRunning: stub macro --
; After patching, CHECK_APP_RUNNING directly calls FORCE_KILL_YUNCAT and the
; original getProcessInfo.nsh / _CHECK_APP_RUNNING path has been removed.
; This stub is kept in case electron-builder or other macros reference it.
!macro customCheckAppRunning
  DetailPrint "Closing running Yuncat..."
  !insertmacro FORCE_KILL_YUNCAT
!macroend

; -- customInstall: parallel extraction of the pre-compressed 7z runtime components --
; Inspired by Docker Desktop / Postman: ship runtimes as 7z archives and extract
; during install. Per-archive splitting enables selective updates and parallel
; 7z decompression (-mmt=8) for faster install.
;
; Archives actually shipped (keep in sync with electron-builder.yml extraResources):
;   python-core.7z      -> resources/python-embed/   (Python runtime + base libs)
;   python-ml.7z        -> resources/python-embed/   (torch/transformers ML stack)
;
; Note: all Python components extract into python-embed/ because their internal
;       paths are relative to that root.

!macro customInstall
  ; -- Skip extraction only when the version matches AND the runtime is really on disk --
  ; 升级安装会先卸载旧版本、清掉 python-embed，而 RuntimeVersion 仍是上一个版本号
  ; （0.1.3 升 0.1.3 时完全相等），只比版本号会让运行时缺失、把 ~90 秒解压全甩给
  ; 首次启动（2026-09-22 用户在真机上遇到：装完第一次打开要等解压）。
  ; 因此必须同时确认 python-embed 里的解释器确实存在。
  ReadRegStr $0 HKCU "Software\Yuncat" "RuntimeVersion"
  ${If} $0 == "${VERSION}"
    IfFileExists "$INSTDIR\resources\python-embed\python.exe" 0 needs_extract
      DetailPrint "Runtime version unchanged ($0) and runtime present, skipping extraction"
      Goto install_done
    needs_extract:
    DetailPrint "Runtime version matches but runtime missing, extracting"
  ${EndIf}

  DetailPrint "Starting parallel runtime extraction..."
  SetDetailsPrint listonly

  ; -- Prepare target directories --
  CreateDirectory "$INSTDIR\resources\python-embed"

  ; -- Parallel extraction of the shipped archives --
  IfFileExists "$INSTDIR\resources\extract-runtime.ps1" 0 skip_extract
    ; -NoProfile avoids loading user profiles that may fail (VS Code official recommendation)
    ; -NonInteractive prevents Read-Host etc. from hanging the installer
    ; -ExecutionPolicy Bypass is required for unsigned install scripts
    nsExec::ExecToLog 'powershell.exe -ExecutionPolicy Bypass -NoProfile -NonInteractive -File "$INSTDIR\resources\extract-runtime.ps1" -TargetDir "$INSTDIR"'
    Pop $1
    ${If} $1 != 0
      DetailPrint "Error: runtime extraction failed (exit code: $1)"
      SetDetailsPrint both
      MessageBox MB_OK|MB_ICONSTOP "Runtime extraction failed (exit code: $1). Installation will abort.$\n$\nArchives are kept; you can re-run the installer to retry."
      Abort "Runtime extraction failed; installation aborted"
    ${EndIf}
  skip_extract:

  ; -- Record runtime version for future upgrade checks --
  WriteRegStr HKCU "Software\Yuncat" "RuntimeVersion" "${VERSION}"
  DetailPrint "Runtime extraction completed, version recorded: ${VERSION}"

  ; -- Detect upgrade scenario --
  ; The uninstaller writes UpgradeUninstall=1 before an upgrade uninstall.
  ; If present, this install is an upgrade and Yuncat should auto-restart afterwards.
  ReadRegStr $R0 HKCU "Software\Yuncat" "UpgradeUninstall"
  ${If} $R0 == "1"
    DetailPrint "Upgrade detected; Yuncat will start after installation"
    DeleteRegValue HKCU "Software\Yuncat" "UpgradeUninstall"
    WriteRegStr HKCU "Software\Yuncat" "AutoRestartAfterUpgrade" "1"
  ${Else}
    ; Fresh install: do not auto-start (finish page lets the user opt in)
    DeleteRegValue HKCU "Software\Yuncat" "AutoRestartAfterUpgrade"
  ${EndIf}

  install_done:
    SetDetailsPrint both

  ; -- Auto-start after upgrade install --
  ReadRegStr $R0 HKCU "Software\Yuncat" "AutoRestartAfterUpgrade"
  ${If} $R0 == "1"
    DetailPrint "Starting Yuncat..."
    ExecShell "" "$INSTDIR\Yuncat.exe" "" SW_SHOWNORMAL
    DeleteRegValue HKCU "Software\Yuncat" "AutoRestartAfterUpgrade"
  ${EndIf}
!macroend

; -- customUnInstall: registry cleanup + optional user data removal --
!macro customUnInstall
  ; First step: close Yuncat before uninstalling files
  DetailPrint "Closing Yuncat..."
  !insertmacro FORCE_KILL_YUNCAT

  ; Mark this uninstall as an upgrade-triggered uninstall so the installer knows
  ; whether to auto-restart after upgrade.
  WriteRegStr HKCU "Software\Yuncat" "UpgradeUninstall" "1"

  ; -- Remove auto-start registry entries --
  ; Electron app.setLoginItemSettings writes to HKCU Run under app.getName().
  ; DeleteRegValue is harmless when the value does not exist.
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Yuncat"
  ; Electron 20+ also writes an enablement marker under StartupApproved\Run
  DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "Yuncat"

  ; -- Remove runtime version registry value --
  DeleteRegValue HKCU "Software\Yuncat" "RuntimeVersion"

  ; -- Remove Windows Defender exclusions possibly left by older installs --
  ; extract-runtime.ps1 no longer adds any (Microsoft/CISA advise against persistent
  ; exclusions on user-writable paths); this stays so upgrades clean up old entries.
  DetailPrint "Removing Windows Defender exclusions..."
  nsExec::ExecToLog 'powershell.exe -ExecutionPolicy Bypass -NoProfile -NonInteractive -Command "try { Remove-MpPreference -ExclusionPath \"$INSTDIR\" -ErrorAction SilentlyContinue } catch { }; try { Remove-MpPreference -ExclusionPath \"$INSTDIR\resources\" -ErrorAction SilentlyContinue } catch { }"'
  Pop $0

  ; -- Ask whether to remove user data directory --
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "Do you want to delete Yuncat user data (settings, chat history, memories)?$\n$\n\
    Yes will permanently remove all data and cannot be undone;$\n\
    No will keep the data for reinstallation." \
    IDYES delete_user_data IDNO keep_user_data

  delete_user_data:
    ; -- 删除顺序与原子性（2026-10-05 用户实例）--
    ;
    ; 不能直接 RMDir /r：Windows 上被杀软 / 索引器 / 仍持有句柄的进程锁住的文件
    ; 删不掉，而 RMDir 会跳过后继续删其余的，于是可能留下「库还在、cred.key 没了」
    ; 的半删状态。下次启动会生成新密钥，用户已保存的 API Key 全部永久解不开
    ; （2026-09-25、2026-10-05 两次实测同一现象：密文加密于 09-22，cred.key 是重装后才建的）。
    ;
    ; 因此按「库 -> 密钥 -> 其余」的顺序删，且每一步删不掉就整体放弃：
    ;   - 库删不掉 -> 立刻停手，密钥留着 -> 库与密钥仍然配对（一致）
    ;   - 库删掉了 -> 再删密钥；即便密钥删不掉，剩下的密钥对空库无害
    ;     （下次启动复用同一把密钥，不会作废任何已存凭据）
    ; 唯一绝不能出现的顺序是「先删密钥、库还在」。

    Delete "$APPDATA\Yuncat\yuncat.db"
    Delete "$APPDATA\Yuncat\yuncat.db-wal"
    Delete "$APPDATA\Yuncat\yuncat.db-shm"
    IfFileExists "$APPDATA\Yuncat\yuncat.db" data_delete_aborted

    Delete "$APPDATA\Yuncat\cred.key"

    ; 其余数据（对话历史 / 记忆 / 窗口状态等）尽力删除；个别文件被锁不影响一致性
    RMDir /r "$APPDATA\Yuncat"
    IfFileExists "$APPDATA\Yuncat\*.*" data_delete_partial
      DetailPrint "Deleted Yuncat user data directory: $APPDATA\Yuncat"
      Goto uninstall_done

    data_delete_partial:
      ; 数据库与密钥已按上面的顺序处理，剩余残留（多为被锁的缓存文件）不影响再次安装使用
      DetailPrint "User data mostly removed; some locked files remain: $APPDATA\Yuncat"
      Goto uninstall_done

    data_delete_aborted:
      ; 库没能删掉 -> 密钥刻意保留，避免出现「库在、密钥没了」的半删状态
      MessageBox MB_OK|MB_ICONEXCLAMATION "Unable to delete Yuncat user data (database file is locked).$\n$\nTo avoid invalidating your saved API keys, the encryption key was left intact and no data was removed.$\n$\nPlease close Yuncat and retry the uninstall, or delete this folder manually:$\n$APPDATA\Yuncat"
      DetailPrint "User data deletion aborted: database file still locked, key kept intact"
      Goto uninstall_done

  keep_user_data:
    DetailPrint "Kept Yuncat user data directory: $APPDATA\Yuncat"

  uninstall_done:
!macroend
