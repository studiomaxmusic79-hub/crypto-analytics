@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo Open http://127.0.0.1:8000 in your browser. Ctrl+C stops the server.
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
goto end
:fail
echo Setup failed. Install Python 3.12 or later and check your Internet connection.
pause
:end
