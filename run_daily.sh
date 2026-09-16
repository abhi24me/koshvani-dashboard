#!/data/data/com.termux/files/usr/bin/bash

# ==========================================
# Koshvani Daily Automation
# ==========================================

cd ~/koshvani-dashboard || exit 1

# ==========================================
# Logging
# ==========================================

LOG_DIR="$HOME/koshvani-dashboard/logs"

mkdir -p "$LOG_DIR"

# One log file per day
LOG_FILE="$LOG_DIR/daily-$(date +%Y-%m-%d).log"

# Delete logs older than 10 days
find "$LOG_DIR" -name "daily-*.log" -type f -mtime +10 -delete

# Send all output to today's log
exec >> "$LOG_FILE" 2>&1


# ==========================================
# Start
# ==========================================

echo ""
echo "========================================"
echo "KOSHVA​​NI JOB STARTED"
echo "Time: $(date)"
echo "========================================"


# ==========================================
# Git Pull
# ==========================================

echo ""
echo "[1/4] Pulling latest code from GitHub..."

git pull --rebase origin main

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Git pull failed."
    echo "Time: $(date)"
    echo "========================================"
    exit 1
fi

echo "Git pull successful."


# ==========================================
# Run Scraper
# ==========================================

echo ""
echo "[2/4] Starting Koshvani scraper..."
echo "Time: $(date)"
echo ""

python scraper/scrape.py

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Koshvani scraper FAILED."
    echo "Time: $(date)"
    echo "========================================"
    exit 1
fi

echo ""
echo "Koshvani scraper completed successfully."


# ==========================================
# Check Data Changes
# ==========================================

echo ""
echo "[3/4] Checking for data changes..."

if git diff --quiet -- docs/data; then

    echo "No data changes detected."
    echo "GitHub update not required."

    echo ""
    echo "========================================"
    echo "KOSH​​VANI JOB COMPLETED"
    echo "Status: SUCCESS - NO DATA CHANGES"
    echo "Time: $(date)"
    echo "========================================"

    exit 0
fi

echo "Data changes detected."


# ==========================================
# Commit Changes
# ==========================================

echo ""
echo "[4/4] Committing and pushing updated data..."

git add docs/data

git commit -m "Update Koshvani data"

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Git commit failed."
    echo "Time: $(date)"
    echo "========================================"
    exit 1
fi

echo "Git commit successful."


# ==========================================
# Push to GitHub
# ==========================================

echo ""
echo "Pushing changes to GitHub..."

git push origin main

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Git push failed."
    echo "Time: $(date)"
    echo "========================================"
    exit 1
fi

echo ""
echo "GitHub push successful."


# ==========================================
# Completed
# ==========================================

echo ""
echo "========================================"
echo "KOSH​​VANI JOB COMPLETED"
echo "Status: SUCCESS"
echo "Time: $(date)"
echo "========================================"

