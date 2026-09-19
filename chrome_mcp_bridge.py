#!/usr/bin/env python3
"""
Chrome MCP Bridge - CDP-based MCP server for Chromium browser control.
Version 2.3.0: navigation previews, bounded text, and native multi-image output.

Architecture:
  MCP Client -> HTTP :9224 (this bridge) -> CDP WebSocket :9223 -> Chromium
"""

import http.server
import json
import urllib.request
import urllib.error
import urllib.parse
import threading
import time
import logging
import sys
import os

from chrome_vision import ChromeVision, TOOL_SCHEMA as VISION_TOOL_SCHEMA

try:
    import websocket
except ImportError:
    print("Error: websocket-client not installed. Run: pip3 install websocket-client")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CDP_HOST = "127.0.0.1"


def _configured_port(name: str, default: int) -> int:
    value = os.environ.get(name, str(default))
    if not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise ValueError(f"{name} must be an integer from 1 to 65535")
    return int(value)


CDP_PORT = _configured_port("CHROME_CDP_PORT", 9223)
# This HTTP endpoint has no auth. Private-interface overrides require ACLs.
BRIDGE_HOST = os.environ.get("CHROME_MCP_HOST", "127.0.0.1")
BRIDGE_PORT = _configured_port("CHROME_MCP_PORT", 9224)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("chrome-mcp-bridge")


# ---------------------------------------------------------------------------
# CDP Client – talks to a single Chromium tab via WebSocket
# ---------------------------------------------------------------------------
class CDPClient:
    """Minimal CDP WebSocket client."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: websocket.WebSocket | None = None
        self._msg_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, str | None] = {}

    def connect(self) -> None:
        self.ws = websocket.create_connection(self.ws_url, timeout=10)
        logger.info("CDP connected to %s", self.ws_url)

    def close(self) -> None:
        if self.ws:
            self.ws.close()
            self.ws = None

    def send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and return the result dict."""
        if not self.ws:
            raise RuntimeError("CDP not connected")
        with self._lock:
            self._msg_id += 1
            msg_id = self._msg_id
            payload = {
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
            self.ws.send(json.dumps(payload))
            # read response
            while True:
                resp = json.loads(self.ws.recv())
                if resp.get("id") == msg_id:
                    if "error" in resp:
                        raise RuntimeError(f"CDP error: {resp['error']}")
                    return resp.get("result", {})
                # else: it's an event or another message, ignore

    def evaluate(self, expression: str, await_promise: bool = False) -> dict:
        """Run JavaScript in the page context."""
        return self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })


# ---------------------------------------------------------------------------
# Helper: find the active Chrome tab via CDP HTTP endpoint
# ---------------------------------------------------------------------------
def get_active_tab() -> dict | None:
    """Return the first 'page' target from CDP /json endpoint."""
    try:
        resp = urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json", timeout=5)
        targets = json.loads(resp.read())
        for t in targets:
            if t.get("type") == "page":
                return t
    except Exception as e:
        logger.error("Failed to fetch CDP target list: %s", e)
    return None


def get_page_content(cdp: CDPClient) -> str:
    """Extract readable text content from current page."""
    script = r"""
    (function() {
        // Try to get article/main content first, fall back to body text
        var article = document.querySelector('article');
        var main = document.querySelector('main');
        var body = document.body;
        var el = article || main || body;
        if (!el) return '';
        // Clone to avoid modifying live page
        var clone = el.cloneNode(true);
        // Remove script and style tags
        var removes = clone.querySelectorAll('script, style, noscript, svg, nav, footer, header, iframe, [hidden], [aria-hidden="true"], .sidebar, .ad, .advertisement');
        removes.forEach(function(n) { n.remove(); });
        var text = clone.innerText || clone.textContent || '';
        // Normalize whitespace
        text = text.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
        // Truncate if very long (max 50000 chars)
        if (text.length > 50000) text = text.substring(0, 50000) + '\n\n... [TRUNCATED]';
        return text;
    })()
    """
    result = cdp.evaluate(script)
    return result.get("result", {}).get("value", "")


def get_page_metadata(cdp: CDPClient) -> dict:
    """Extract page metadata (title, description, author, language)."""
    script = r"""
    (function() {
        var meta = {};
        meta.title = document.title || '';
        var desc = document.querySelector('meta[name="description"]');
        meta.description = desc ? desc.getAttribute('content') : '';
        var author = document.querySelector('meta[name="author"]');
        meta.author = author ? author.getAttribute('content') : '';
        meta.language = document.documentElement.lang || '';
        return meta;
    })()
    """
    result = cdp.evaluate(script)
    value = result.get("result", {}).get("value", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {"title": value}
    return value


vision = ChromeVision(
    cdp_factory=CDPClient, get_active_tab=get_active_tab, host=CDP_HOST, port=CDP_PORT
)


# ---------------------------------------------------------------------------
# MCP HTTP Handler
# ---------------------------------------------------------------------------
class MCPHandler(http.server.BaseHTTPRequestHandler):
    """Handles MCP JSON-RPC over HTTP."""

    protocol_version = "HTTP/1.0"

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send_json({"error": "use POST"}, 405)

    def do_POST(self) -> None:
        try:
            body_len = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(body_len)) if body_len else {}
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json({"jsonrpc": "2.0", "error": {"code": -32700, "message": str(e)}}, 400)
            return

        method = body.get("method", "")
        req_id = body.get("id", 0)

        # --- MCP protocol handshake ---
        if method == "initialize":
            self._send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "ChromeMCP-CDP",
                        "version": "2.3.0",
                    },
                },
            })
            return

        if method == "notifications/initialized":
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return

        # --- tools/list ---
        if method == "tools/list":
            self._send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        VISION_TOOL_SCHEMA,
                        {
                            "name": "chrome_navigate",
                            "description": "只打开一次指定URL并返回page_id。小红书笔记/分享链接默认附带图片ID清单、标题正文和当前可见评论；拿到ID后直接用chrome_view mode=images批量看图，无需再list。只做本地有界等待，不刷新、翻图或加载评论。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "url": {
                                        "type": "string",
                                        "description": "要打开的网页URL",
                                    },
                                    "wait_ms": {"type": "integer", "minimum": 0, "maximum": 15000, "default": 12000,
                                                "description": "笔记本地加载等待预算，默认12秒，已就绪会提前返回；不是整次网络调用超时。"},
                                    "include_note": {"type": "boolean", "default": True,
                                                     "description": "小红书链接是否附带笔记清单；默认开启。"},
                                },
                                "required": ["url"],
                                "additionalProperties": False,
                            },
                        },
                        {
                            "name": "chrome_get_web_content",
                            "description": "获取网页文本、标题与元数据。优先传page_id避免串页。图片不能从文本读出；用chrome_view mode=images按已知ID批量回传，小红书navigate/list已附带笔记正文。",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "url": {
                                        "type": "string",
                                        "description": "可选：先导航到此URL再获取内容",
                                    },
                                    "page_id": {"type": "string", "description": "可选：准确读取此标签页，与url互斥。"},
                                },
                                "additionalProperties": False,
                            },
                        },
                    ],
                },
            })
            return

        # --- tools/call ---
        if method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            if not isinstance(args, dict):
                self._send_json({"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": -32602, "message": "arguments must be an object"}})
                return
            tool_result = self._execute_tool(tool_name, args)
            # Vision must remain native MCP ImageContent. Wrapping it in text
            # would turn the actual pixels into an unreadable base64 string.
            result = tool_result if isinstance(tool_result, dict) else {
                "content": [{"type": "text", "text": tool_result}],
            }
            self._send_json({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            })
            return

        # --- unknown ---
        self._send_json({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        })

    def _execute_tool(self, name: str, args: dict) -> str | dict:
        """Execute a tool and return the text result."""
        if name == "chrome_view":
            return vision.call(args)
        if name == "chrome_navigate":
            if set(args) - {"url", "wait_ms", "include_note"}:
                return self._text_result({"error": "invalid_arguments", "message": "Unknown navigate argument."}, error=True)
            return self._chrome_navigate(args.get("url", ""), args.get("wait_ms", 12000), args.get("include_note", True))

        if name == "chrome_get_web_content":
            if set(args) - {"url", "page_id"} or ("url" in args and "page_id" in args):
                return self._text_result({"error": "invalid_arguments", "message": "Choose url or page_id, not both."}, error=True)
            target_url = args.get("url", "")
            page_id = args.get("page_id")
            if "page_id" in args and (not isinstance(page_id, str) or not page_id):
                return self._text_result({"error": "invalid_arguments", "message": "Invalid page_id."}, error=True)
            if target_url:
                nav_result = self._chrome_navigate(target_url, include_note=False)
                if nav_result.get("isError"):
                    return nav_result
                page_id = json.loads(nav_result["content"][0]["text"])["page_id"]
                return self._text_result({"navigation_status": "opened", "page_id": page_id,
                    "content_result": self._chrome_get_web_content(page_id),
                    "note": "网页已经打开；即使正文读取失败也不要重复带url导航。后续使用此page_id。"})

            return self._chrome_get_web_content(page_id)

        return f"Unknown tool: {name}"

    @staticmethod
    def _text_result(data, error=False):
        result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
        if error:
            result["isError"] = True
        return result

    def _chrome_navigate(self, url: str, wait_ms=12000, include_note=True) -> dict:
        """Open exactly once. A preview error must never retry or erase the tab receipt."""
        try:
            parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
            valid_url = parsed and parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid_url = False
        if not valid_url or len(url) > 16000 or any(ord(c) < 32 for c in url):
            return self._text_result({"error": "invalid_arguments", "message": "Provide an HTTP(S) URL without embedded credentials."}, error=True)
        if type(wait_ms) is not int or not 0 <= wait_ms <= 15000 or type(include_note) is not bool:
            return self._text_result({"error": "invalid_arguments", "message": "wait_ms must be 0..15000 integer; include_note must be boolean."}, error=True)

        try:
            req = urllib.request.Request(
                f"http://{CDP_HOST}:{CDP_PORT}/json/new?{urllib.parse.quote(url, safe='')}",
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                tab_info = json.loads(r.read())
            if not isinstance(tab_info, dict) or not tab_info.get("id"):
                raise ValueError("No tab acknowledgement")
        except Exception:
            logger.warning("Navigation acknowledgement unavailable; no retry performed.")
            return self._text_result({"error": "navigation_unconfirmed", "navigation_status": "unconfirmed",
                "message": "未能确认打开网页；未重试或改动其他标签页。先人工检查浏览器，不要反复导航。"}, error=True)

        result = {"mode": "navigate", "navigation_status": "opened", "page_id": tab_info["id"],
                  "title": tab_info.get("title", ""), "preview_status": "not_requested",
                  "note": "页面已打开。后续使用这个page_id，不要重复导航。"}
        host = parsed.hostname.lower()
        is_note = any(host == domain or host.endswith("." + domain) for domain in ("xiaohongshu.com", "xhslink.com", "xhslink.cn"))
        if include_note and is_note:
            try:
                preview = vision.preview_opened_page(tab_info["id"], wait_ms)
                payload = json.loads(preview["content"][0]["text"])
                result["note_preview"] = payload
                result["preview_status"] = "error" if preview.get("isError") else payload.get("state", "unknown")
                content = payload.get("note_content") or {}
                if content.get("title"):
                    result["title"] = content["title"]
            except Exception:
                result["preview_status"] = "unavailable"
                result["note"] = "页面已打开，但笔记预览不可用。保留page_id，先检查页面，不要重复导航。"
            return self._text_result(result)

        cdp = None
        try:
            cdp = CDPClient(tab_info["webSocketDebuggerUrl"])
            cdp.connect()
            time.sleep(1)
            result["title"] = cdp.evaluate("document.title").get("result", {}).get("value", "")
        except Exception:
            logger.warning("New tab acknowledged; title unavailable. No navigation retry.")
        finally:
            if cdp is not None:
                try:
                    cdp.close()
                except Exception:
                    logger.warning("Title connection close failed; navigation receipt retained.")
        return self._text_result(result)

    def _chrome_get_web_content(self, page_id=None) -> str:
        """Get full page content using CDP."""
        try:
            tab = vision._page(page_id)
        except Exception:
            return "错误: 指定的网页标签页不可用；未切换到其他标签页"
        if not tab:
            return "错误: 没有活跃的网页标签页"

        ws_url = tab.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return "错误: 无法获取页面调试URL"

        try:
            cdp = CDPClient(ws_url)
            cdp.connect()
            try:
                cdp.send("Page.enable")
                cdp.send("Runtime.enable")

                # Get metadata
                meta = get_page_metadata(cdp)
                title = meta.get("title", tab.get("title", ""))
                url = tab.get("url", "")

                # Get text content
                text_content = get_page_content(cdp)

                # Build response
                lines = []
                lines.append(f"URL: {url}")
                lines.append(f"标题: {title}")
                if meta.get("description"):
                    lines.append(f"描述: {meta['description']}")
                if meta.get("author"):
                    lines.append(f"作者: {meta['author']}")
                if meta.get("language"):
                    lines.append(f"语言: {meta['language']}")
                lines.append("")
                lines.append("--- 页面正文 ---")
                lines.append(text_content if text_content else "(无法提取页面内容)")

                return "\n".join(lines)
            finally:
                cdp.close()
        except Exception as e:
            logger.error("get_web_content error: %s", e)
            return f"错误: 获取页面内容失败 - {e}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if BRIDGE_HOST not in ("127.0.0.1", "localhost"):
        logger.warning("MCP is binding beyond loopback without authentication. Restrict access with a firewall / tailnet ACL; never expose it publicly.")
    # Check that CDP is reachable
    logger.info("Checking CDP endpoint %s:%d ...", CDP_HOST, CDP_PORT)
    for attempt in range(5):
        tab = get_active_tab()
        if tab:
            logger.info("CDP OK – found tab: %s", tab.get("title", "?"))
            break
        logger.info("Waiting for CDP... attempt %d/5", attempt + 1)
        time.sleep(2)
    else:
        logger.warning("No CDP tab found. Make sure Chromium is running with --remote-debugging-port=%d", CDP_PORT)

    # Start HTTP server
    import socketserver

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    server = ReusableTCPServer((BRIDGE_HOST, BRIDGE_PORT), MCPHandler)
    logger.info("Chrome MCP Bridge listening on %s:%d", BRIDGE_HOST, BRIDGE_PORT)
    logger.info("CDP target: %s:%d", CDP_HOST, CDP_PORT)
    logger.info("Available tools: chrome_navigate, chrome_get_web_content, chrome_view")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
