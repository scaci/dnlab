@echo off
setlocal EnableExtensions

set "SCHEME=dnlab-capture"
set "SELF=%~f0"
set "CMD=%~1"

if /I "%CMD%"=="install" goto install
if /I "%CMD%"=="uninstall" goto uninstall
if /I "%CMD%"=="doctor" goto doctor
if /I "%CMD%"=="open" goto open
echo Usage: %~nx0 install^|uninstall^|doctor^|open ^<dnlab-capture-url^>
pause
exit /b 2

:install
reg add "HKCU\Software\Classes\%SCHEME%" /ve /d "URL:DNLab Capture" /f >nul
reg add "HKCU\Software\Classes\%SCHEME%" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\%SCHEME%\shell\open\command" /ve /d "\"%SELF%\" open \"%%1\"" /f >nul
echo Registered %SCHEME%:// for this Windows user.
pause
exit /b 0

:uninstall
reg delete "HKCU\Software\Classes\%SCHEME%" /f >nul 2>nul
echo Unregistered %SCHEME%:// for this Windows user.
pause
exit /b 0

:doctor
call :find_wireshark
echo Handler: %SELF%
if defined WIRESHARK (
  echo Wireshark: %WIRESHARK%
  exit /b 0
)
echo Wireshark: not found
exit /b 1

:open
set "HANDLER_URL=%~2"
if not defined HANDLER_URL (
  echo Missing dnlab-capture URL.
  pause
  exit /b 2
)
call :find_wireshark
if not defined WIRESHARK (
  echo Wireshark not found. Install Wireshark or add it to PATH.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$u=[uri]$env:HANDLER_URL; $q=@{}; foreach($part in $u.Query.TrimStart('?').Split('&')){ if(-not $part){continue}; $kv=$part.Split('=',2); $k=[uri]::UnescapeDataString($kv[0]); $v=''; if($kv.Length -gt 1){$v=[uri]::UnescapeDataString($kv[1].Replace('+',' '))}; $q[$k]=$v }; $status=$q['status_url']; $stream=$q['stream_url']; if(-not $status -or -not $stream){throw 'Missing status_url or stream_url'}; try { $pre=Invoke-RestMethod -Uri $status -Method Get -ErrorAction Stop } catch { Write-Host 'DNLab capture preflight request failed.'; Write-Host ('Status URL: {0}' -f $status); Write-Host ('Error: {0}' -f $_.Exception.Message); exit 1 }; if(-not $pre.ok){ $code=$pre.code; if(-not $code){$code='capture_error'}; $detail=$pre.detail; if(-not $detail){$detail='Capture is not available'}; Write-Host ('DNLab capture preflight failed [{0}]: {1}' -f $code,$detail); exit 1 }; $psi=New-Object System.Diagnostics.ProcessStartInfo; $psi.FileName=$env:WIRESHARK; $psi.Arguments='-k -i -'; $psi.UseShellExecute=$false; $psi.RedirectStandardInput=$true; $p=[System.Diagnostics.Process]::Start($psi); $in=$p.StandardInput.BaseStream; $resp=[System.Net.HttpWebRequest]::Create($stream).GetResponse(); $s=$resp.GetResponseStream(); $buf=New-Object byte[] 131072; try { while(-not $p.HasExited){ $ar=$s.BeginRead($buf,0,$buf.Length,$null,$null); while(-not $ar.AsyncWaitHandle.WaitOne(500)){ if($p.HasExited){ $resp.Close(); $in.Close(); exit $p.ExitCode } }; $n=$s.EndRead($ar); if($n -le 0){break}; if($p.HasExited){break}; $in.Write($buf,0,$n); $in.Flush() } } finally { try{$in.Close()}catch{}; try{$s.Close()}catch{}; try{$resp.Close()}catch{} }; if(-not $p.HasExited){$p.WaitForExit()}; exit $p.ExitCode"
if errorlevel 1 (
  echo DNLab capture failed.
  pause
  exit /b 1
)
exit /b 0

:find_wireshark
set "WIRESHARK="
if exist "%ProgramFiles%\Wireshark\Wireshark.exe" set "WIRESHARK=%ProgramFiles%\Wireshark\Wireshark.exe"
if not defined WIRESHARK if exist "%ProgramFiles(x86)%\Wireshark\Wireshark.exe" set "WIRESHARK=%ProgramFiles(x86)%\Wireshark\Wireshark.exe"
if not defined WIRESHARK for /f "delims=" %%W in ('where wireshark.exe 2^>nul') do if not defined WIRESHARK set "WIRESHARK=%%W"
exit /b 0
