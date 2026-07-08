@echo off
setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "VENV_PYW=.venv\Scripts\pythonw.exe"
set "REBUILD_VENV="

if not exist "%VENV_PY%" set "REBUILD_VENV=1"

if not defined REBUILD_VENV (
    "%VENV_PY%" -c "import sys" >nul 2>nul
    if errorlevel 1 set "REBUILD_VENV=1"
)

if defined REBUILD_VENV (
    if exist ".venv" (
        echo Wykryto uszkodzone srodowisko .venv - odbudowuje...
        rmdir /s /q ".venv"
        if exist ".venv" goto :error
    ) else (
        echo Pierwsze uruchomienie - przygotowuje srodowisko aplikacji...
    )

    py -3 -m venv .venv
    if errorlevel 1 goto :error
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 goto :error
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
)

start "" "%VENV_PYW%" app.py
exit /b 0

:error
echo.
echo Nie udalo sie przygotowac aplikacji.
echo Sprawdz polaczenie z internetem i instalacje Python 3.
pause
exit /b 1
