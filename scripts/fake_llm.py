"""Mock LLM 回放假件（M3.2/M3.3）：OpenAI 兼容 /chat/completions 记录-回放服务器。

零 API 费地驱动 Hermes：把 config.toml [llm] 指到本服务（base_url=http://127.0.0.1:<port>/v1），
它把收到的每个请求（messages、tools、stream）原样记入 jsonl 日志，并按脚本回放应答——
脚本为空时返回固定文本。支持流式（SSE）与非流式；支持 tool_calls 回放（M3.3 编排用）。

回放条目的工具参数支持回填占位符 "${re:<pattern>}"：pattern 在本请求收到的工具结果文本
（role=tool 消息）中搜索，命中则以匹配文本替换（M3.3 全流程：save_result 的 final 需要
finish 实际返回的绝对路径，真 LLM 从工具结果里读，回放同口径）。未命中则原样保留。
匹配文本若以引号开头（Hermes 把 MCP 工具结果包成 JSON 转义的 {"result": "..."} 文本），
剥引号并还原 JSON 转义（成对反斜杠还原为单个）后再替换。

用法：
  uv run python scripts/fake_llm.py --port 8765 --log 路径.jsonl [--script 回放.jsonl] [--model 假名]
回放脚本每行：{"content": "文本"} 或 {"content": "...", "tool_calls": [{"name": "工具名", "arguments": {...}}]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ReplayEmpty(RuntimeError):
    """脚本耗尽：用于让调用方结束会话（Hermes 收到报错后停止）。"""


class Replay:
    """按序弹出回放条目；缺省返回固定文本。"""

    def __init__(self, script: Path | None, fallback: str) -> None:
        self.entries: list[dict] = []
        if script is not None:
            for line in script.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.entries.append(json.loads(line))
        self.fallback = fallback

    def next(self) -> dict:
        if self.entries:
            return self.entries.pop(0)
        return {"content": self.fallback}

    def message(self, model: str, request: dict | None = None) -> dict:
        entry = self.next()
        message: dict = {"role": "assistant", "content": entry.get("content", "")}
        tool_calls = entry.get("tool_calls")
        if tool_calls:
            message["content"] = entry.get("content") or None
            message["tool_calls"] = [
                {"id": f"call_{index}", "type": "function",
                 "function": {"name": call["name"],
                              "arguments": self._fill(call.get("arguments", {}), request or {})}}
                for index, call in enumerate(tool_calls)
            ]
        return {"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
                "model": model, "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    @staticmethod
    def _tool_texts(request: dict) -> str:
        out: list[str] = []
        for m in request.get("messages", []):
            if m.get("role") != "tool":
                continue
            content = m.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.append("".join(b.get("text", "") for b in content if isinstance(b, dict)))
        return "\n".join(out)

    def _fill(self, arguments: dict, request: dict) -> str:
        """参数序列化 + "${re:<pattern>}" 回填（工具结果文本中取值，见模块 docstring）。

        占位符只出现在字符串值里，在序列化**前**替换：json.dumps 会把 pattern 的
        反斜杠转义成 \\S，匹配就失效了。
        """
        blob = self._tool_texts(request)

        def fix(value):
            if isinstance(value, str):
                def sub(match: re.Match) -> str:
                    found = re.search(match.group(1), blob)
                    if not found:
                        return match.group(0)
                    text = found.group(0)
                    if text.startswith('"'):
                        # Hermes 把 MCP 工具结果包成 {"result": "<JSON 转义文本>"}：
                        # 路径匹配会带进开引号与 \\ 双反斜杠，剥引号并还原转义。
                        text = text[1:].rstrip('"').replace("\\\\", "\\")
                    return text
                return re.sub(r"\$\{re:([^}]+)\}", sub, value)
            if isinstance(value, dict):
                return {key: fix(item) for key, item in value.items()}
            if isinstance(value, list):
                return [fix(item) for item in value]
            return value

        return json.dumps(fix(arguments), ensure_ascii=False)


class Handler(BaseHTTPRequestHandler):
    server_version = "fake_llm/1.0"
    replay: Replay
    log_path: Path
    model_name: str
    status_only: bool = False  # 强制应答 status 调用且不消费回放（M4.4 真 kill E2E：take 落位后开启，保活到被 kill，回放零消耗不错位）

    def log_message(self, fmt, *args) -> None:  # 静默访问日志，避免刷屏
        pass

    def _reply(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._reply({"object": "list",
                         "data": [{"id": self.model_name, "object": "model"}]})
            return
        self._reply({"error": {"message": f"unsupported GET {self.path}"}}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._reply({"error": {"message": "invalid JSON"}}, 400)
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._reply({"error": {"message": f"unsupported POST {self.path}"}}, 404)
            return
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps({"t": time.time(), "request": request}, ensure_ascii=False) + "\n")
        try:
            if self.status_only:
                response = {"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
                            "model": request.get("model", self.model_name),
                            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                "role": "assistant", "content": None,
                                "tool_calls": [{"id": "call_0", "type": "function",
                                                "function": {"name": "mcp__viggle_remake__status",
                                                             "arguments": json.dumps({"sid": self._last_sid(request)})}}]}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            else:
                response = self.replay.message(request.get("model", self.model_name), request)
        except Exception as exc:  # 记录回放故障，保持服务器存活
            self._reply({"error": {"message": f"replay failure: {exc}"}}, 500)
            return
        if request.get("stream"):
            self._stream(response)
        else:
            self._reply(response)

    @staticmethod
    def _last_sid(request: dict) -> str:
        """从请求历史的工具调用参数里取最近的 sid（status_only 应答用）。"""
        for message in reversed(request.get("messages", [])):
            for call in message.get("tool_calls") or []:
                try:
                    sid = json.loads(call["function"]["arguments"]).get("sid")
                except (ValueError, KeyError, TypeError):
                    continue
                if sid:
                    return sid
        return "unknown"

    def _stream(self, response: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        choice = response["choices"][0]
        message = choice["message"]
        deltas: list[dict] = [{"role": message["role"]}]
        if message.get("content"):
            deltas.append({"content": message["content"]})
        for call in message.get("tool_calls") or []:
            deltas.append({"tool_calls": [call]})
        base = {key: response[key] for key in ("id", "object", "created", "model")}
        base["object"] = "chat.completion.chunk"
        for delta in deltas:
            chunk = {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        stop = {**base, "choices": [{"index": 0, "delta": {},
                                     "finish_reason": choice["finish_reason"]}]}
        self.wfile.write(f"data: {json.dumps(stop)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenAI 兼容 mock LLM 回放服务器")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", required=True, help="请求日志 jsonl 路径")
    parser.add_argument("--script", default=None, help="回放脚本 jsonl（缺省回固定文本）")
    parser.add_argument("--reply", default="OK", help="无脚本时的固定应答文本")
    parser.add_argument("--model", default="fake-llm")
    args = parser.parse_args(argv)

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    Handler.replay = Replay(Path(args.script) if args.script else None, args.reply)
    Handler.log_path = Path(args.log)
    Handler.model_name = args.model
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[fake_llm] listening on 127.0.0.1:{args.port}；日志：{args.log}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
