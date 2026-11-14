@echo off
rem Rebuild installer. Requires: D:\oopz\7za.exe, .NET csc v4.
rem Layout: this dir (dist) holds installer.cs + this bat + MuppetStudio-Setup.exe (committed for distribution).
rem App sources are the parent dir (muppet_studio); models live in muppet_studio\models (gitignored).
rem IMPORTANT: comments here are ASCII-only. cmd reads .bat as ANSI(GBK) here, and UTF-8 Chinese
rem in rem lines gets misparsed and breaks the script.
setlocal
set "HERE=%~dp0"
set "APP=%HERE%.."
set "CSC=C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
set "TMPDIR=%TEMP%\muppet_build_%RANDOM%"
mkdir "%TMPDIR%" >nul 2>&1
rem -xr!dist: never zip the installer output (49MB exe) into itself, else Setup becomes ~100MB
rem -xr!legacy: legacy/v1 is a git-only archive of the retired V1 app, not shipped with the installer
rem -xr!models\sam: sam is now an asset-managed download (~40MB), not shipped inside the installer
rem -xr!runtime: runtime ships as its own embedded resource (see RT_RESOURCE below), not inside app zip
rem -xr!webgal_runtime: downloaded WebGAL runtime (MPL-2.0, ~50MB) is user-side asset, NEVER shipped
rem   (note: -xr!runtime above matches the dir literally named "runtime" only, NOT webgal_runtime!)
D:\oopz\7za.exe a -tzip -mx=9 "%TMPDIR%\muppet_app.zip" "%APP%\*" -xr!24yearsold -xr!models\sam -xr!runtime -xr!webgal_runtime -xr!dist -xr!legacy -xr!__pycache__ -xr!launcher.log -xr!comfy_launch.log -xr!recent_projects.json -xr!.git -xr!seethrough_out -bd >nul
if errorlevel 1 goto :err
pushd "%APP%"
"D:\Python312\python.exe" -c "import os,json;d=os.path.join('models','24yearsold');fs=[{'repo':'24yearsold/'+r,'file':os.path.relpath(os.path.join(dp,f),os.path.join(d,r)).replace(chr(92),chr(47)),'size':os.path.getsize(os.path.join(dp,f))} for dp,dn,fn in os.walk(d) for f in fn for r in [dp.split(os.sep)[len(d.split(os.sep))]]] if os.path.isdir(d) else [];json.dump(sorted(fs,key=lambda x:(x['repo'],x['file'])),open(r'%TMPDIR%\model_manifest.json','w',encoding='utf8'),ensure_ascii=False,indent=1)" 2>nul
popd
if not exist "%TMPDIR%\model_manifest.json" goto :err
copy /y "%HERE%installer.cs" "%TMPDIR%\" >nul
rem - inject app version from l2d_rig_qt.py VERSION into the installer title
"D:\Python312\python.exe" "%HERE%inject_version.py" "%APP%\l2d_rig_qt.py" "%TMPDIR%\installer.cs"

copy /y "%APP%\anon_sewing_icon.ico" "%TMPDIR%\" >nul
copy /y "%HERE%installer_banner.png" "%TMPDIR%\" >nul
rem - runtime.zip: portable Python runtime (python.exe + Lib + site-packages), embedded when present
rem   so a fresh install can launch immediately without the user having Python installed.
if exist "D:\build\runtime.zip" copy /y "D:\build\runtime.zip" "%TMPDIR%\runtime.zip" >nul
set RT_RESOURCE=
if exist "%TMPDIR%\runtime.zip" set RT_RESOURCE=/resource:runtime.zip
pushd "%TMPDIR%"
"%CSC%" /nologo /target:winexe /win32icon:anon_sewing_icon.ico /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll /r:System.Management.dll /resource:muppet_app.zip /resource:model_manifest.json /resource:installer_banner.png %RT_RESOURCE% /out:MuppetStudio-Setup.exe installer.cs
if errorlevel 1 popd & goto :err
popd
copy /y "%TMPDIR%\MuppetStudio-Setup.exe" "%HERE%MuppetStudio-Setup.exe" >nul
rmdir /s /q "%TMPDIR%"
echo BUILD OK
endlocal
goto :eof
:err
echo BUILD FAILED
endlocal
exit /b 1
