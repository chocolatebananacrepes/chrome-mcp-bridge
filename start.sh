#!/bin/sh
echo "[start] Launching headless Chromium (CDP port 9223)..."
chromium \
  --headless=new \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9223 \
  --remote-allow-origins=http://127.0.0.1:9223 \
  --user-data-dir=/tmp/chrome-mcp-profile \
  about:blank &

for i in $(seq 1 30); do
  if python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:9223/json/version', timeout=1)" 2>/dev/null; then
    echo "[start] Chromium CDP ready"
    break
  fi
  sleep 1
done

export CHROME_MCP_HOST=0.0.0.0
export CHROME_MCP_PORT=8080
export CHROME_CDP_PORT=9223

echo "[start] Starting Chrome MCP Bridge on 0.0.0.0:8080 ..."
exec python chrome_mcp_bridge.py
