#!/bin/bash
echo "========================================================"
echo "  AI Job Hunter - Browser Launcher (macOS / Linux)"
echo "========================================================"

if command -v python3 >/dev/null 2>&1; then
    echo "Launching managed Chrome via python..."
    python3 -m jobagent browser && exit 0
fi

PORT=9222
USER_DIR="${HOME}/.local/share/ai-job-hunter/chrome-profile"

if [[ "$OSTYPE" == "darwin"* ]]; then
    CHROME_EXE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    USER_DIR="${HOME}/Library/Application Support/AIJobHunter/chrome-profile"
else
    CHROME_EXE=$(which google-chrome || which google-chrome-stable || which chromium || which chromium-browser)
fi

if [ -z "$CHROME_EXE" ] || [ ! -f "$CHROME_EXE" ]; then
    echo "[Error] Google Chrome not found! Please install Google Chrome."
    exit 1
fi

mkdir -p "$USER_DIR"
echo "Starting Chrome with debug port $PORT..."
"$CHROME_EXE" --remote-debugging-port=$PORT --remote-debugging-address=127.0.0.1 --remote-allow-origins=http://127.0.0.1:$PORT,http://localhost:$PORT --user-data-dir="$USER_DIR" --no-first-run --no-default-browser-check "https://www.zhipin.com/web/geek/chat" &
echo "Started! Please log in to your account and keep the window open."
