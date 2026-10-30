@echo off
setlocal
cd /d "%~dp0"

echo Starting completion archive checker...
echo If the browser does not open automatically, visit http://localhost:8501
echo Keep this window open. Close it to stop the app.
echo.

python -m streamlit run app.py %*

if errorlevel 1 (
    echo.
    echo Startup failed. Check that Python is installed and dependencies are ready:
    echo     pip install -r requirements.txt
    echo.
    pause
)
