@echo off
rem Scheduled task WebtoonStudio-XgenCatalogSync (every 10 min): re-register agent platform catalog only when vast workflows/address changed.
rem ASCII only (cmd reads this file in the ANSI code page).
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist "%~dp0..\state\logs" mkdir "%~dp0..\state\logs"
"%~dp0..\.venv\Scripts\python.exe" "%~dp0xgen_catalog.py" sync >> "%~dp0..\state\logs\xgen_catalog_sync.log" 2>&1
