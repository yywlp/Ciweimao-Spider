@echo off
cd /d "%~dp0"
if exist "E:\Python3.12.10\pythonw.exe" (
    start "" "E:\Python3.12.10\pythonw.exe" gui.py
) else (
    start "" pyw -3 gui.py
)