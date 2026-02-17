@echo off
setlocal
cd /d "%~dp0"

set "UI_LOG=%TEMP%\simple_moozic_builder_ui_error.log"
if exist "%UI_LOG%" del /q "%UI_LOG%" >nul 2>nul

echo Launching Simple Moozic Builder UI...
python simple_moozic_builder_ui.py 2>"%UI_LOG%"
if errorlevel 1 (
  echo.
  echo UI launch failed. Falling back to CLI...
  if exist "%UI_LOG%" (
    echo.
    echo ---- UI Error Log ----
    type "%UI_LOG%"
    echo ----------------------
  )
  python simple_moozic_builder.py
)
if errorlevel 1 (
  echo.
  echo Builder failed.
  pause
  endlocal
  exit /b 1
)
echo.
echo Builder finished.
pause
endlocal
