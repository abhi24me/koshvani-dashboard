#!/data/data/com.termux/files/usr/bin/bash

cd ~/koshvani-dashboard || exit 1

LOG_DIR="$HOME/koshvani-dashboard/logs"
LOG_FILE="$LOG_DIR/daily.log"

mkdir -p "$LOG_DIR"

exec >> "$LOG_FILE" 2>&1

echo ""
echo "========================================"
echo "Koshvani job started: $(date)"
echo "========================================"

echo "Running git pull..."
git pull --rebase origin main

if [ $? -ne 0 ]; then
    echo "ERROR: Git pull failed: $(date)"
    echo "========================================"
    exit 1
fi

echo "Starting scraper..."
python scraper/scrape.py

if [ $? -ne 0 ]; then
    echo "ERROR: Scraper FAILED: $(date)"
    echo "========================================"
    exit 1
fi

echo "Scraper completed successfully."

if git diff --quiet -- docs/data; then
    echo "No data changes detected."
    echo "Koshvani job completed: $(date)"
    echo "========================================"
    exit 0
fi

echo "Data changes detected."

echo "Committing changes..."
git add docs/data
git commit -m "Update Koshvani data"

if [ $? -ne 0 ]; then
    echo "ERROR: Git commit failed: $(date)"
    echo "========================================"
    exit 1
fi

echo "Pushing changes to GitHub..."
git push origin main

if [ $? -ne 0 ]; then
    echo "ERROR: Git push failed: $(date)"
    echo "========================================"
    exit 1
fi

echo "GitHub push successful."
echo "Koshvani job completed successfully: $(date)"
echo "========================================"
