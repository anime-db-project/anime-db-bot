@echo off
cd /d "%~dp0"
"%~dp0anime-db-bot.exe" start %*
if errorlevel 1 pause
