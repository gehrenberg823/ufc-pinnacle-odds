@echo off
cd /d "%~dp0"
echo.
echo   UFC feed eval - Bolt vs Kalshi
echo   Starting server... your browser will open at http://localhost:8899
echo   (leave this window open during the fights; press Ctrl+C to stop)
echo.
start "" /min cmd /c "timeout /t 3 >nul & start http://localhost:8899"
node ufc-eval.mjs
pause
