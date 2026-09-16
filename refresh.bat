@echo off
echo Scraping fresh data from koshvani.up.nic.in...
python scraper\scrape.py
if errorlevel 1 (
  echo.
  echo Scrape failed - see error above. Not pushing.
  pause
  exit /b 1
)

echo.
echo Pushing to GitHub...
git add docs\data
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "Update Koshvani data %date% %time%"
  git push
  echo.
  echo Done - dashboard will update in a minute or two.
) else (
  echo.
  echo No changes - data is already up to date.
)
pause
