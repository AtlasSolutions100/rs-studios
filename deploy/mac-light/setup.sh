#!/bin/bash
# Sets up Campaignify's light-model worker on this Mac: Ollama with a local model, and a launchd job that keeps
# worker.py running. Safe to run again. See README.md.
set -euo pipefail
MODEL="${CFY_LIGHT_MODEL:-qwen3:14b}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/campaignify-light"
ENVF="$HOME/.campaignify-light.env"
PLIST="$HOME/Library/LaunchAgents/ai.campaignify.light.plist"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

command -v brew >/dev/null || { echo "Install Homebrew first: https://brew.sh"; exit 1; }
command -v ollama >/dev/null || brew install ollama
brew services start ollama >/dev/null
for i in $(seq 1 20); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
echo "Pulling $MODEL (about 9 GB the first time)..."
ollama pull "$MODEL"

mkdir -p "$DEST"
cp "$HERE/worker.py" "$DEST/worker.py"

if [ ! -f "$ENVF" ] || ! grep -q '^LIGHT_WORKER_TOKEN=.\+' "$ENVF"; then
  echo
  echo "Paste the light worker token (the same value you set as the GitHub secret LIGHT_WORKER_TOKEN)."
  echo "It isn't shown as you paste."
  read -r -s -p "Token: " TOKEN; echo
  [ -n "$TOKEN" ] || { echo "No token given; run this again when you have it."; exit 1; }
  umask 077
  printf 'CFY_LIGHT_URL=https://campaignify.ai\nCFY_LIGHT_MODEL=%s\nLIGHT_WORKER_TOKEN=%s\n' "$MODEL" "$TOKEN" > "$ENVF"
  unset TOKEN
fi
chmod 600 "$ENVF"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.campaignify.light</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-c</string>
    <string>set -a; . "$ENVF"; set +a; exec /usr/bin/env python3 "$DEST/worker.py"</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DEST/worker.log</string>
  <key>StandardErrorPath</key><string>$DEST/worker.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
echo
echo "Running. The last lines of its log:"
tail -n 5 "$DEST/worker.log" || true
echo
echo "Check it any time with: tail -f $DEST/worker.log"
