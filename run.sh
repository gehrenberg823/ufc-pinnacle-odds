#!/bin/zsh
# Refresh the UFC Pinnacle dashboard and push to GitHub if it changed.
# Driven by launchd (com.gregehrenberg.ufc-pinnacle-odds). The scraper auto-picks
# the soonest upcoming card, so this rolls forward to the next card on its own.
set -uo pipefail

DIR="/Users/gregehrenberg/UFC"
PY="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
GIT="/usr/bin/git"
export PATH="/usr/bin:/bin:/usr/local/bin"
cd "$DIR" || exit 1

STAMP="$(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "[$STAMP] refresh start"

if ! "$PY" refresh.py; then
  echo "[$STAMP] refresh.py failed"
  exit 1
fi

# Commit + push only when something actually changed (avoids empty noise commits).
if [[ -n "$("$GIT" status --porcelain index.html)" ]]; then
  "$GIT" add -A
  "$GIT" -c user.name="gehrenberg823" -c user.email="gehrenberg@awesemo.com" \
    commit -qm "auto-refresh odds $STAMP"
  if "$GIT" push -q origin main; then
    echo "[$STAMP] pushed update"
  else
    echo "[$STAMP] push failed"
  fi
else
  echo "[$STAMP] no change"
fi
