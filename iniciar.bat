@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Tradutor ao vivo
".venv\Scripts\python.exe" server.py
pause
