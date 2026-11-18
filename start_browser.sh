#!/bin/bash
echo "========================================================"
echo "  AI Job Hunter - Browser Launcher (macOS / Linux)"
echo "========================================================"

PORT=9222
USER_DIR="$(pwd)/data/chrome-profile"

if [[ "$OSTYPE" == "darwin"* ]]; then
    CHROME_EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
else
    CHROME_EXE=$(which google-chrome || which google-chrome-stable || which chromium)
fi

if [ -z "$CHROME_EXE" ] || [ ! -f "$CHROME_EXE" ]; then
    echo "[Error] Chrome browser not found! Please install Google Chrome."
    exit 1
fi

echo "Starting Chrome with debug port $PORT..."
"$CHROME_EXE" --remote-debugging-port=$PORT --user-data-dir="$USER_DIR" "https://www.zhipin.com/web/geek/chat" &
echo "Started! Please log in to your account and keep the window open."
