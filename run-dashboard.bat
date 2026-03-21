@echo off
cd /d "%~dp0dashboard"
if exist "node_modules\electron\dist\electron.exe" (
    start "" "node_modules\electron\dist\electron.exe" .
) else (
    start "" cmd /c "npx electron ."
)
