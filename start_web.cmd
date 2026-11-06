@echo off
rem Start the local web app and open it in the browser (Windows).
rem Check first: py -3.13 scripts\check_env.py
cd /d "%~dp0"
start "" http://127.0.0.1:8128/make
py -3.13 scripts\serve.py
pause
