#!/usr/bin/env bash
# Persist the bot's state (bot_state.json, paper_trades.csv, signals.csv) between
# GitHub Actions runs by committing it to a separate branch, so `main` stays clean.
#   scripts/state_sync.sh pull   -> checks out the state branch into ./state
#   scripts/state_sync.sh push   -> commits ./state and pushes it
set -euo pipefail
BRANCH="${STATE_BRANCH:-goldbot-state}"
DIR="${STATE_DIR:-state}"

case "${1:-}" in
  pull)
    git config user.name "goldbot"
    git config user.email "goldbot@users.noreply.github.com"
    rm -rf "$DIR"
    if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
      git fetch --depth=1 origin "$BRANCH"
      git worktree add --detach "$DIR" FETCH_HEAD
    else
      echo "state branch '$BRANCH' does not exist yet; starting fresh"
      git worktree add --detach "$DIR"
      git -C "$DIR" checkout --orphan "$BRANCH"
      git -C "$DIR" rm -rfq . || true
    fi
    ;;
  push)
    cd "$DIR"
    git add -A
    if git diff --cached --quiet; then
      echo "state unchanged"
      exit 0
    fi
    git -c user.name=goldbot -c user.email=goldbot@users.noreply.github.com \
      commit -qm "goldbot state $(date -u +%Y-%m-%dT%H:%MZ)"
    for i in 1 2 3 4; do
      git push origin "HEAD:refs/heads/$BRANCH" && exit 0
      sleep $((2 ** i))
    done
    echo "failed to push state" >&2
    exit 1
    ;;
  *)
    echo "usage: $0 pull|push" >&2
    exit 2
    ;;
esac
