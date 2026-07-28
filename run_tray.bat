@echo off
REM Launch Hermes Cost Dashboard Tray App
REM Point this at your dashboard URL
set COST_API_URL=https://cost.omoikane.icu
set COST_REFRESH_INTERVAL=5m
start "" "%~dp0hermes-cost-tray.exe"
