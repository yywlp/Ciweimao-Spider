@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "E:\Python3.12.10\python.exe" (
    "E:\Python3.12.10\python.exe" ocr.py %*
) else (
    py -3 ocr.py %*
)
echo.
pause