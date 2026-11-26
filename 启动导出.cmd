@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "E:\Python3.12.10\python.exe" (
    "E:\Python3.12.10\python.exe" main.py %*
) else (
    py -3 main.py %*
)
echo.
pause
