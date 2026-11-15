@echo off
chcp 65001 >nul
setlocal
echo Starting DirectorLab ComfyUI setup...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_comfyui_windows.ps1" %*
if errorlevel 1 (
  echo.
  echo Setup failed. Please take a screenshot of the errors above.
  pause
)
endlocal
