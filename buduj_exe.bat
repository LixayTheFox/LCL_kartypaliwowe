@echo off
setlocal
cd /d "%~dp0"

echo Buduje samodzielna wersje EXE aplikacji Fuel Insight...

if not exist ".venvScriptspython.exe" (
    echo Tworze lokalne srodowisko Python...
    py -3 -m venv .venv
    if errorlevel 1 goto :error
)

".venvScriptspython.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error

".venvScriptspython.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

".venvScriptspython.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto :error

".venvScriptspython.exe" -m PyInstaller --noconfirm --clean --windowed --onefile --name FuelInsight --distpath wydanie --workpath build app.py
if errorlevel 1 goto :error

echo.
echo Gotowe.
echo Plik dla uzytkownika: %cd%\wydanie\FuelInsight.exe
echo Mozesz skopiowac sam ten plik na inny komputer z Windows.
pause
exit /b 0

:error
echo.
echo Nie udalo sie zbudowac pliku EXE.
echo Sprawdz komunikat powyzej i polaczenie z internetem.
pause
exit /b 1

