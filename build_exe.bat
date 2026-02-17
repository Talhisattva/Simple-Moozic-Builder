@echo off
setlocal
cd /d "%~dp0"

echo Checking PyInstaller...
python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo PyInstaller not found. Installing...
  python -m pip install pyinstaller
  if errorlevel 1 (
    echo.
    echo Failed to install PyInstaller.
    pause
    endlocal
    exit /b 1
  )
)

echo Building Simple Moozic Builder (one-folder EXE)...
python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --windowed ^
  --name "SimpleMoozicBuilder" ^
  --icon "icon.ico" ^
  --add-data "assets;assets" ^
  --add-data "icon.ico;." ^
  --add-data "icon32.png;." ^
  simple_moozic_builder_ui.py

if errorlevel 1 (
  echo.
  echo Build failed.
  pause
  endlocal
  exit /b 1
)

echo.
echo Build complete.
echo Output: dist\SimpleMoozicBuilder\

if exist "ffmpeg\ffmpeg.exe" (
  if not exist "dist\SimpleMoozicBuilder\ffmpeg" mkdir "dist\SimpleMoozicBuilder\ffmpeg"
  copy /y "ffmpeg\ffmpeg.exe" "dist\SimpleMoozicBuilder\ffmpeg\ffmpeg.exe" >nul
  if exist "ffmpeg\LICENSE*" copy /y "ffmpeg\LICENSE*" "dist\SimpleMoozicBuilder\ffmpeg\" >nul
  if exist "ffmpeg\README*" copy /y "ffmpeg\README*" "dist\SimpleMoozicBuilder\ffmpeg\" >nul
  echo Bundled ffmpeg from .\ffmpeg\ffmpeg.exe
) else (
  echo NOTE: No local .\ffmpeg\ffmpeg.exe found. Conversion will require system ffmpeg in PATH.
)

echo IMPORTANT: Run the EXE from dist\SimpleMoozicBuilder\ (NOT build\SimpleMoozicBuilder\)
if exist "dist\SimpleMoozicBuilder" start "" "dist\SimpleMoozicBuilder"
pause
endlocal
