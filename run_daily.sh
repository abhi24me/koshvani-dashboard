#!/data/data/com.termux/files/usr/bin/bash

# ==========================================
# Koshvani Daily Automation
# ==========================================

REPO="$HOME/koshvani-dashboard"
LOG_DIR="$REPO/logs"

cd "$REPO" || exit 1

# ==========================================
# Daily Log
# ==========================================

mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/daily-$(date +%Y-%m-%d).log"

# Delete logs older than 10 days
find "$LOG_DIR" -type f -name "daily-*.log" -mtime +10 -delete

# Show output on screen AND save to log
exec > >(tee -a "$LOG_FILE") 2>&1

echo ""
echo "========================================"
echo "KOSHVANI JOB STARTED"
echo "Time: $(date)"
echo "========================================"


# ==========================================
# Check Git Status
# ==========================================

echo ""
echo "[1/5] Checking Git status..."

git status --short

# ==========================================
# Pull Latest Code
# ==========================================

echo ""
echo "[2/5] Pulling latest code from GitHub..."

git pull --rebase origin main

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Git pull failed."
    echo "Please check Git status."
    echo "Time: $(date)"
    echo "========================================"
    exit 1
fi

echo "Git pull successful."


# ==========================================
# Run Koshvani Scraper
# ==========================================

echo ""
echo "[3/5] Starting Koshvani scraper..."
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
echo "[4/5] Checking for data changes..."

if git diff --quiet -- docs/data; then

    echo "No data changes detected."
    echo "No GitHub update required."

    echo ""
    echo "========================================"
    echo "KOSHVANI JOB COMPLETED"
    echo "Status: SUCCESS - NO DATA CHANGES"
    echo "Time: $(date)"
    echo "========================================"

    exit 0
fi

echo "Data changes detected."


# ==========================================
# Commit & Push
# ==========================================

echo ""
echo "[5/5] Committing updated Koshvani data..."

git add docs/data

git commit -m "Update Koshvani data"

if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Git commit failed."
    echo "Time: $(date)"
    echo "========================================"
    exit 1
fi

echo "Commit successful."

echo ""
echo "Pushing to GitHub..."

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
# Finished
# ==========================================

echo ""
echo "========================================"
echo "KOSHVANI JOB COMPLETED"
echo "Status: SUCCESS"
echo "Time: $(date)"
echo "========================================"
