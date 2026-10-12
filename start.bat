@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0tui.py" %*
    goto :done
)
where uv >nul 2>nul
if errorlevel 1 (
    echo Install uv, then run: uv sync --extra mcp
    exit /b 1
)
uv run --project "%~dp0." --extra mcp python "%~dp0tui.py" %*
:done
exit /b %errorlevel%
