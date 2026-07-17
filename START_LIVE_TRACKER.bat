@echo off
title Kyiv Live Tracker
echo Starting live proxy on :8902 and web server on :8901...
start "live-proxy" cmd /c "C:\Users\Basyl\miniconda3\python.exe "%~dp0scripts\live_proxy.py""
start "web" cmd /c "C:\Users\Basyl\miniconda3\python.exe -m http.server 8901 --directory "%~dp0.""
timeout /t 2 >nul
start http://localhost:8901/kyiv_live_tracker.html
echo Close both console windows to stop.
