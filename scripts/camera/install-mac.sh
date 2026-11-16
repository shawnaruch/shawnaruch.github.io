#!/bin/bash
# Install the furnace camera job on this Mac (run on the Mac mini, on the home network).
#
#   curl -fsSL https://raw.githubusercontent.com/shawnaruch/shawnaruch.github.io/master/scripts/camera/install-mac.sh | bash
#
# Safe to re-run: it updates the script and reloads the timer, and never
# overwrites your settings file.
set -euo pipefail

APP_DIR="$HOME/furnace-cam"
CFG_DIR="$HOME/.config/furnace-cam"
LABEL="com.esemay.furnace-cam"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RAW="https://raw.githubusercontent.com/shawnaruch/shawnaruch.github.io/master/scripts/camera"
INTERVAL="${FURNACE_CAM_INTERVAL:-900}"   # seconds between checks (900 = 15 min)

echo "==> Checking tools"
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh  (then re-run this installer)"; exit 1
fi
command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg
command -v git >/dev/null 2>&1 || { echo "git missing: run  xcode-select --install"; exit 1; }
PY="$(command -v python3)"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || brew install python
PY="$(command -v python3)"

echo "==> Installing to $APP_DIR"
mkdir -p "$APP_DIR" "$CFG_DIR" "$HOME/Library/LaunchAgents"
curl -fsSL "$RAW/furnace_cam.py" -o "$APP_DIR/furnace_cam.py"
[ -d "$APP_DIR/venv" ] || "$PY" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip anthropic

if [ ! -f "$CFG_DIR/env" ]; then
  cat > "$CFG_DIR/env" <<'EOF'
# Furnace camera settings. Keep this file private (chmod 600). Never commit it.
CAMERA_URL=rtsp://USER:PASS@192.168.4.109:554/live0
ANTHROPIC_API_KEY=
GITHUB_TOKEN=
NTFY_TOPIC=
# Stronger reader for a faint display, ~40x the cost: remove the "# " below to enable
# CAMERA_MODEL=claude-opus-5-5
EOF
  echo "==> Created $CFG_DIR/env  <-- fill this in, then re-run the test below"
fi
chmod 600 "$CFG_DIR/env"

echo "==> Installing timer (every $((INTERVAL / 60)) min)"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$APP_DIR/venv/bin/python</string><string>$APP_DIR/furnace_cam.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$CFG_DIR/run.log</string>
  <key>StandardErrorPath</key><string>$CFG_DIR/run.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "Done. Next:"
echo "  1. Edit settings (plain-text editor, not TextEdit):   nano $CFG_DIR/env"
echo "  2. Test (no publish):  $APP_DIR/venv/bin/python $APP_DIR/furnace_cam.py --dry-run"
echo "  3. Watch the log:   tail -f $CFG_DIR/run.log"
echo "  Keep the Mac mini awake: System Settings > Energy > 'Prevent automatic sleeping' on."
