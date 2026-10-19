@echo off
rem webtoon-studio MCP over HTTP (127.0.0.1:7440/mcp). Started at boot by scheduled task WEBTOON-MCP-http.
rem Public: https://webtoon.js-96.com/mcp via Cloudflare tunnel + Access service token. See mcp/README.md.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set WEBTOON_PUBLIC_URL=https://webtoon.js-96.com
cd /d D:\webtoon-studio
"D:\webtoon-studio\.venv\Scripts\python.exe" mcp\server.py --http --port 7440 >> state\mcp_http.log 2>&1
