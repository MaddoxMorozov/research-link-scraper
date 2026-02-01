@echo off
TITLE Research Link Scraper Service
echo --- Research Link Scraper Service ---
echo Mode: Production
echo Logging to: scraping_log.log
echo.
cd /d "%~dp0"
python main_service.py
pause
