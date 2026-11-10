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
D:\oopz\7za.exe a -tzip -mx=9 "%TMPDIR%\muppet_app.zip" "%APP%\*" -xr!24yearsold -xr!dist -xr!legacy -xr!__pycache__ -xr!launcher.log -xr!comfy_launch.log -xr!recent_projects.json -xr!.git -xr!seethrough_out -bd >nul
if errorlevel 1 goto :err
pushd "%APP%"
"D:\Python312\python.exe" -c "import os,json;d=os.path.join('models','24yearsold');fs=[{'repo':'24yearsold/'+r,'file':os.path.relpath(os.path.join(dp,f),os.path.join(d,r)).replace(chr(92),chr(47)),'size':os.path.getsize(os.path.join(dp,f))} for dp,dn,fn in os.walk(d) for f in fn for r in [dp.split(os.sep)[len(d.split(os.sep))]]] if os.path.isdir(d) else [];json.dump(sorted(fs,key=lambda x:(x['repo'],x['file'])),open(r'%TMPDIR%\model_manifest.json','w',encoding='utf8'),ensure_ascii=False,indent=1)" 2>nul
popd
if not exist "%TMPDIR%\model_manifest.json" goto :err
copy /y "%HERE%installer.cs" "%TMPDIR%\" >nul
copy /y "%APP%\laowu_icon.ico" "%TMPDIR%\" >nul
pushd "%TMPDIR%"
"%CSC%" /nologo /target:winexe /win32icon:laowu_icon.ico /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll /r:System.Management.dll /resource:muppet_app.zip /resource:model_manifest.json /out:MuppetStudio-Setup.exe installer.cs
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
