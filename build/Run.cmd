@echo off
cd /d "%~dp0"
"%~dp0anime-db-bot.exe" run %*
if errorlevel 1 pause
