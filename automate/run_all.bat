@echo off
REM Runs Person A (make clips) then Person B (upload). Task Scheduler runs this file.
REM Edit TOPIC below. Output is appended to automate\run.log
REM Free Upload-Post plan = 10 posts a month, so this posts 1 clip per run.
REM Schedule it every 3 days (about 10 a month).

set TOPIC=GTA 6
cd /d "%~dp0.."

echo ===== %date% %time% ===== >> automate\run.log
py automate\make_clips.py --topic "%TOPIC%" >> automate\run.log 2>&1
py automate\upload.py --max-uploads 1 >> automate\run.log 2>&1
