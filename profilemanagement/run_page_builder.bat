@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.8 "personal_page_builder.py"
if errorlevel 1 (
  echo.
  echo Launch failed. Please check that Python 3.8 with tkinter and Pillow is installed.
  pause
)
