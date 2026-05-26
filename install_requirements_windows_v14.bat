@echo off
title Install Image Video Harvester x Matcher V14 Requirements
echo Installing Image/Video Harvester x Matcher Python requirements...
python -m pip install --upgrade pip
python -m pip install -r requirements_image_video_harvester_x_matcher_v14.txt
python -m playwright install chromium
echo.
echo NOTE: MP4 conversion requires FFmpeg.
echo If ffmpeg is not installed, run:
echo   winget install Gyan.FFmpeg
echo Or download FFmpeg and set the path in the GUI.
echo.
echo Done. Double-click launch_gui_windows_v14.bat
pause
