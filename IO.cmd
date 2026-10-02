@echo off
rem Opens IO (or brings its window forward if it is already running), starting the boss model
rem and loading UI-TARS in Unsloth Studio if they are not already running.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" desktop.py
