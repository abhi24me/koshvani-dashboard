#!/data/data/com.termux/files/usr/bin/env bash
# ==========================================
# Koshvani Daily Automation
# ==========================================
#
# Two halves, one file (the whole file is parsed before anything runs, so a
# `git pull` that replaces it mid-run cannot corrupt the run that pulled it):
#
#   run_outer     creates a NEW log for this execution (logs/daily-YYYY-MM-DD-HHMMSS.log),
#                 runs the workflow with all its output piped through logsafe.py (nothing
#                 sensitive can reach the log or the screen), and - once that pipeline has
#                 completely drained - commits and pushes the finished log. That happens on
#                 every exit path: success, partial run, no data changes, early failure.
#   run_workflow  the actual job: pull, scrape, commit + push the data, send the Gmail report.
#
# Only explicit paths are ever staged (docs/data, and the execution logs) - never the whole tree.

log()  { echo "$(date '+%Y-%m-%d %H:%M:%S') INFO  [run_daily] $*"; }
warn() { echo "$(date '+%Y-%m-%d %H:%M:%S') WARN  [run_daily] $*"; }

# git_step LABEL COMMAND...   runs a git command and logs its start, exit code and elapsed time
git_step() {
    local label="$1"; shift
    local began; began=$(date +%s)
    log "GIT $label: START"
    "$@"
    local rc=$?
    log "GIT $label: END exit_code=$rc elapsed=$(( $(date +%s) - began ))s"
    return $rc
}

# ---------------------------------------------------------------------------
# Guarding a run: one at a time, and awake for as long as it takes
# ---------------------------------------------------------------------------

LOCK_DIR_PATH=""
WAKE_LOCK_HELD=""
LOCK_MAX_AGE_MIN=720       # a lock older than this is never trusted, whatever its pid says

# lock_is_live LOCK_DIR: true only if a genuine, recent run_daily.sh still holds it.
# A pid alone proves very little: Android recycles them, so a lock left behind by a
# run that was killed can end up pointing at some unrelated live process - and
# trusting that would make every later run quietly exit as "already running",
# forever, with no log written at all. So the holder only counts if it is alive,
# is really a run_daily.sh (where /proc can tell us), and the lock is not ancient.
lock_is_live() {
    local lock_dir="$1" pid cmdline
    pid=$(cat "$lock_dir/pid" 2>/dev/null)
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        return 1                                   # nobody is there any more
    fi
    if [ -n "$(find "$lock_dir" -maxdepth 0 -mmin +"$LOCK_MAX_AGE_MIN" 2>/dev/null)" ]; then
        return 1                                   # far too old to be a real run
    fi
    if [ -r "/proc/$pid/cmdline" ]; then
        cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
        case "$cmdline" in
            *run_daily.sh*) ;;
            *) return 1 ;;                         # the pid was recycled by something else
        esac
    fi
    return 0
}

# cleanup_on_exit: runs however the script ends (a normal exit, an error, SIGTERM).
cleanup_on_exit() {
    [ -n "$LOCK_DIR_PATH" ] && rm -rf "$LOCK_DIR_PATH"
    [ -n "$WAKE_LOCK_HELD" ] && termux-wake-unlock >/dev/null 2>&1
    return 0
}

# acquire_lock: refuses to let two run_daily.sh executions touch the same git
# working tree at once. A double-trigger (a stuck previous run plus a fresh
# scheduled one, or the scheduler itself firing twice) is exactly how
# logs/daily-2026-10-16-192731.log ended up uncommitted on the phone on 16 Oct,
# which then blocked every single `git pull --rebase` for the next six days -
# nothing else in this script could repair a dirty file outside docs/data, so
# the crawler never ran again until that file was manually cleared. This lock
# makes a second overlapping run exit immediately instead of racing the first.
# A plain `mkdir` (atomic, no extra package - works unchanged on Termux) is the
# lock. A skipped run writes no execution log (it must not touch the repository),
# so it leaves a one-line trace in $HOME instead, where a missing day can be explained.
acquire_lock() {
    local lock_dir="$HOME/.koshvani-dashboard.lock" holder
    if ! mkdir "$lock_dir" 2>/dev/null; then
        holder=$(cat "$lock_dir/pid" 2>/dev/null)
        if lock_is_live "$lock_dir"; then
            warn "another run_daily.sh (pid $holder) is already running - exiting without touching the repository"
            echo "$(date '+%Y-%m-%d %H:%M:%S') skipped: run_daily.sh pid $holder still holds the lock" >> "$HOME/.koshvani-dashboard.skips.log"
            exit 0
        fi
        warn "a stale lock from pid ${holder:-?} (no longer a running run_daily.sh) was left behind - taking it over"
        rm -rf "$lock_dir"
        mkdir "$lock_dir" || { warn "could not create the lock directory - exiting"; exit 1; }
    fi
    echo $$ > "$lock_dir/pid"
    LOCK_DIR_PATH="$lock_dir"
    trap cleanup_on_exit EXIT
}

# acquire_wake_lock: on Termux, Android freezes the CPU - and the network with it -
# whenever the screen is off and the phone sits idle. That is what wrecked the run of
# 31 Oct: its log has twenty silent gaps of 10 to 160 minutes in the middle of one
# scrape (a 41-minute job that took 15 hours of real time, with connections dropped
# on every wake-up and even the final `git push` taking 2 h 42 min before failing).
# `termux-wake-lock` ships with Termux's stock termux-tools - no extra package - and
# keeps the phone awake for as long as this run lasts; cleanup_on_exit releases it.
# Anywhere else the command does not exist and this is simply skipped.
acquire_wake_lock() {
    if command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock >/dev/null 2>&1; then
        WAKE_LOCK_HELD=1
        export KOSHVANI_WAKE_LOCK="held (the phone cannot suspend this run)"
    fi
}

# ==========================================
# The workflow (runs as the "inner" process)
# ==========================================

run_workflow() {

REPO="$HOME/koshvani-dashboard"
cd "$REPO" || exit 1

START_TS="${KOSHVANI_START_TS:-$(date +%s)}"

echo ""
log "========================================"
log "KOSHVANI JOB STARTED"
log "execution_id=$KOSHVANI_EXECUTION_ID log=$KOSHVANI_LOG_FILE"
log "host: $(uname -sr) | $(python --version 2>&1) | $(git --version)"
log "wake lock: ${KOSHVANI_WAKE_LOCK:-not held (this device can suspend a long run)}"
log "repo: branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) HEAD=$(git rev-parse --short HEAD 2>/dev/null) dir=$REPO"
log "========================================"


# ==========================================
# Check Git Status
# ==========================================

echo ""
log "[1/6] Checking Git status..."

git status --short

STATUS_FILE="docs/data/crawler_status.json"
if [ -f "$STATUS_FILE" ] && grep -q '"overall_status": "RUNNING"' "$STATUS_FILE"; then
    warn "NOTE: the previous crawler execution never finished (its status file still says RUNNING)."
fi

# docs/data is machine-generated by scrape.py, and the scraper saves every
# scheme the moment it is scraped - so uncommitted changes left by an
# interrupted run (lost signal, Termux killed by Android, ...) are real,
# successfully scraped data, not junk. They used to be discarded here purely so
# that `git pull --rebase` would not refuse to run over unstaged changes; saving
# them as a commit satisfies the pull just as well without destroying anything.
if [ -n "$(git status --porcelain -- docs/data)" ]; then
    warn "Found uncommitted docs/data changes from an interrupted run."
    log "Saving them as a commit instead of discarding them..."
    git_step "add leftover data" git add docs/data
    git_step "commit leftover data" git commit -m "Save partial Koshvani data from an interrupted run"
    if [ $? -ne 0 ]; then
        warn "could not commit the leftover data - leaving it untouched."
    fi
fi

# An already-published execution log is made read-only right after it is
# committed (see publish_log below) and is never meant to change again, so a
# LOCALLY MODIFIED one is never legitimate new data to preserve - it was
# already fully committed and pushed once. This is exactly what happened to
# logs/daily-2026-10-16-192731.log on 16 Oct (two overlapping executions - see
# acquire_lock above, which now stops that - raced on this same working tree
# and left one of them with a stray local edit to an already-published log),
# and it then blocked every single `git pull --rebase` for the next six days,
# because nothing here used to resolve a dirty file outside docs/data. The fix
# is simply to discard the stray edit and restore the log to the content that
# was already safely committed - never to recommit whatever ended up on disk
# locally (that would skip the sanitizer check publish_log always runs first).
STALE_LOGS=()
while IFS= read -r f; do
    [ -n "$f" ] && STALE_LOGS+=("$f")
done < <(git diff --name-only -- 'logs/daily-????-??-??-??????*.log' 'logs/daily-????-??-??.log')
if [ ${#STALE_LOGS[@]} -gt 0 ]; then
    warn "A previously published execution log was found locally modified - discarding the stray change(s): ${STALE_LOGS[*]}"
    git_step "restore modified log(s)" git checkout -- "${STALE_LOGS[@]}"
fi

# ==========================================
# Pull Latest Code
# ==========================================

echo ""
log "[2/6] Pulling latest code from GitHub..."

git_step "pull --rebase" git pull --rebase origin main

if [ $? -ne 0 ]; then
    echo ""
    warn "ERROR: Git pull failed."
    warn "Please check Git status."
    # A pull that stopped half way leaves a rebase in progress, which would make every later run fail too.
    GIT_DIR_PATH="$(git rev-parse --git-dir 2>/dev/null)"
    if [ -d "$GIT_DIR_PATH/rebase-merge" ] || [ -d "$GIT_DIR_PATH/rebase-apply" ]; then
        warn "the failed pull left a rebase in progress - aborting it so the repository is usable again"
        git_step "rebase --abort" git rebase --abort
    fi
    log "FINAL STATUS: ERROR (git pull failed)"
    python gmail_alert.py error --start "$START_TS" --stage git-pull
    exit 1
fi

log "Git pull successful."


# ==========================================
# Run Koshvani Scraper
# ==========================================

echo ""
log "[3/6] Starting Koshvani scraper..."

SCRAPER_BEGAN=$(date +%s)
python scraper/scrape.py
SCRAPER_RC=$?
log "SCRAPER END exit_code=$SCRAPER_RC elapsed=$(( $(date +%s) - SCRAPER_BEGAN ))s"

# The scraper exits non-zero only when the run was cut short (a fatal error,
# or it was interrupted). Every scheme it finished was already saved to disk,
# so that is never a reason to throw the data away: it is committed and
# pushed below, and the report says PARTIAL / ERROR.
echo ""
if [ $SCRAPER_RC -eq 0 ]; then
    log "Koshvani scraper completed."
else
    warn "the scraper stopped early (exit code $SCRAPER_RC)."
    log "Everything it had already saved is kept and published below."
fi


# ==========================================
# Check Data Changes
# ==========================================

echo ""
log "[4/6] Checking for data changes..."

# `git status --porcelain` (not `git diff`) so a brand-new, still untracked
# file such as the first crawler_status.json counts as a change too.
if [ -z "$(git status --porcelain -- docs/data)" ]; then

    log "No data changes detected."
    log "No GitHub update required."

    # Nothing left that could still fail before the run is done - safe to
    # validate results and notify now.
    echo ""
    log "Validating scheme results and sending the Gmail report..."
    python gmail_alert.py report --start "$START_TS"
    log "GMAIL report step finished (exit_code=$?)"

    echo ""
    log "========================================"
    if [ $SCRAPER_RC -eq 0 ]; then
        log "KOSHVANI JOB COMPLETED"
        log "FINAL STATUS: SUCCESS - NO DATA CHANGES"
    else
        log "KOSHVANI JOB ENDED EARLY"
        log "FINAL STATUS: INCOMPLETE - scraper exit code $SCRAPER_RC"
    fi
    log "elapsed=$(( $(date +%s) - START_TS ))s"
    log "========================================"

    [ $SCRAPER_RC -eq 0 ] && exit 0
    exit 1
fi

log "Data changes detected:"
git status --short -- docs/data


# ==========================================
# Commit & Push
# ==========================================

echo ""
log "[5/6] Committing updated Koshvani data..."

COMMIT_MESSAGE="Update Koshvani data"
if [ $SCRAPER_RC -ne 0 ]; then
    COMMIT_MESSAGE="Update Koshvani data (partial run - scraper stopped early)"
fi

git_step "add data" git add docs/data

git_step "commit data" git commit -m "$COMMIT_MESSAGE"

if [ $? -ne 0 ]; then
    echo ""
    warn "ERROR: Git commit failed."
    log "FINAL STATUS: ERROR (git commit failed)"
    python gmail_alert.py error --start "$START_TS" --stage git-commit
    exit 1
fi

log "Commit successful: $(git log -1 --format='%h %s')"
git show --shortstat --format= HEAD

echo ""
log "Pushing to GitHub..."

git_step "push data" git push origin main

if [ $? -ne 0 ]; then
    echo ""
    warn "ERROR: Git push failed."
    log "FINAL STATUS: ERROR (git push failed)"
    python gmail_alert.py error --start "$START_TS" --stage git-push
    exit 1
fi

echo ""
log "GitHub push successful: HEAD=$(git rev-parse --short HEAD) origin/main=$(git rev-parse --short origin/main)"


# ==========================================
# Validate Results & Notify
# ==========================================

# Only reached once the run is fully complete (pull, scraper, commit and
# push all done) - so the notification always reflects the real final
# outcome, never a SUCCESS sent before a later step could still fail.
echo ""
log "[6/6] Validating scheme results and sending the Gmail report..."

python gmail_alert.py report --start "$START_TS"
log "GMAIL report step finished (exit_code=$?)"


# ==========================================
# Finished
# ==========================================

echo ""
log "========================================"
if [ $SCRAPER_RC -eq 0 ]; then
    log "KOSHVANI JOB COMPLETED"
    log "FINAL STATUS: SUCCESS"
else
    log "KOSHVANI JOB ENDED EARLY"
    log "FINAL STATUS: INCOMPLETE - partial data was published (scraper exit code $SCRAPER_RC)"
fi
log "elapsed=$(( $(date +%s) - START_TS ))s"
log "========================================"

[ $SCRAPER_RC -eq 0 ] && exit 0
exit 1

}


# ==========================================
# Publishing the execution log (runs in the "outer" process, after the workflow)
# ==========================================

publish_log() {
    cd "$REPO" || return 1
    echo ""
    log "LOG PUBLISH: committing and pushing the execution log"

    # Every finished execution log that is not in git yet: this run's, plus any a
    # previous run could not publish (no network, killed...), plus a single-file daily
    # log left by the previous version of this script. Only files matching this script's
    # naming schemes; each is staged by its own name.
    local files=() f
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        python "$REPO/logsafe.py" --scrub-file "$f"          # last line of defence, in place
        if python "$REPO/logsafe.py" --check "$f"; then
            files+=("$f")
        else
            warn "$f still failed the sanitizer check - NOT publishing it"
        fi
    done < <(git ls-files --others --exclude-standard -- 'logs/daily-????-??-??-??????*.log' 'logs/daily-????-??-??.log')

    if [ ${#files[@]} -eq 0 ]; then
        warn "no execution log to publish"
        return 0
    fi

    git_step "add execution log(s)" git add -- "${files[@]}"
    git_step "commit execution log(s)" git commit -m "Add execution log $KOSHVANI_EXECUTION_ID" -- "${files[@]}"
    if [ $? -ne 0 ]; then
        warn "could not commit the execution log(s); they stay on disk and go out with the next run"
        return 1
    fi

    git_step "push execution log" git push origin main
    if [ $? -ne 0 ]; then
        warn "the log push failed - pulling and retrying once"
        git_step "pull --rebase (retry)" git pull --rebase origin main \
            && git_step "push execution log (retry)" git push origin main
        if [ $? -ne 0 ]; then
            warn "the execution log could not be pushed; it stays committed locally and goes out with the next push"
            python "$REPO/gmail_alert.py" error --start "$KOSHVANI_START_TS" --stage log-push
            return 1
        fi
    fi
    chmod a-w "${files[@]}" 2>/dev/null       # a finished execution's log is never edited again
    log "LOG PUBLISH: pushed ${files[*]} (origin/main=$(git rev-parse --short origin/main))"
}

run_outer() {
    acquire_lock
    acquire_wake_lock
    REPO="$HOME/koshvani-dashboard"
    LOG_DIR="$REPO/logs"
    cd "$REPO" || exit 1

    export KOSHVANI_START_TS
    KOSHVANI_START_TS=$(date +%s)
    local stamp; stamp=$(date +%Y-%m-%d-%H%M%S)
    export KOSHVANI_EXECUTION_ID
    KOSHVANI_EXECUTION_ID="$(date +%Y%m%d-%H%M%S)-$(printf '%04x%02x' "$RANDOM" $((RANDOM % 256)))"

    mkdir -p "$LOG_DIR"
    # One log per execution, never reused: the file is created exclusively (noclobber), and
    # a second run in the same second gets a "-2" suffix instead of touching the first one's log.
    LOG_FILE="$LOG_DIR/daily-$stamp.log"
    local n=1
    while ! ( set -o noclobber; : > "$LOG_FILE" ) 2>/dev/null; do
        n=$((n + 1))
        if [ $n -gt 50 ]; then echo "cannot create an execution log in $LOG_DIR" >&2; exit 1; fi
        LOG_FILE="$LOG_DIR/daily-$stamp-$n.log"
    done
    export KOSHVANI_LOG_FILE="logs/$(basename "$LOG_FILE")"

    # Everything the workflow prints (stdout and stderr) passes through the sanitizer on its way
    # to both the screen and the log file; when the pipeline ends the log is complete.
    KOSHVANI_INNER=1 bash "$REPO/run_daily.sh" 2>&1 | python -u "$REPO/logsafe.py" | tee -a "$LOG_FILE"
    local workflow_rc=${PIPESTATUS[0]}

    # The log cannot describe the commit that contains it, so its last line announces that step;
    # what the step itself prints (below) only goes to the screen.
    log "LOG PUBLISH: workflow_exit_code=$workflow_rc - this log is now being committed and pushed (that outcome is on screen only)" | python -u "$REPO/logsafe.py" | tee -a "$LOG_FILE" >/dev/null
    publish_log 2>&1 | python -u "$REPO/logsafe.py"
    return $workflow_rc
}

if [ "${KOSHVANI_INNER:-}" = "1" ]; then
    run_workflow
else
    run_outer
fi
exit $?
