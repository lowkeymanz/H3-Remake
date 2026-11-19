"""假 painter 假件（M3.3/M4.1）：OpenAI 兼容 /images/edits、/images/generations 记录-应答服务器。

零 API 费地替身 MiniMax-H3-Image：把 MCP 注册 env 的 OPENAI_BASE_URL 指到本服务
（http://127.0.0.1:<port>/v1），epaint/paint/sheet 的 images API 调用即落到此处。
应答为一张按请求 size 的纯色 PNG（MCP 端只做导入/对比展示，不校验内容），颜色随调用
计数微扰——每次应答字节不同，重绘（force=true）才能改变 take_fp 触发真正重渲（M4.3 审查-重试）；
每个请求的字段与图片文件名记入 jsonl 日志，供工具调用序列断言。

用法：
  uv run python scripts/fake_painter.py --port 8766 --log 路径.jsonl [--colour 128,64,32]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image


def make_png(size: str, colour: tuple[int, int, int]) -> str:
    """按 "WxH"（缺省 1024x1024，非法值回落）生成纯色 PNG 的 b64。"""
    try:
        width, height = (max(1, int(x)) for x in size.lower().split("x"))
    except ValueError:
        width, height = 1024, 1024
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, list[str]]:
    """multipart/form-data -> (fields, [图片文件名])；不落盘，只取证字段。"""
    fields: dict[str, str] = {}
    images: list[str] = []
    for marker in ("boundary=",):
        if marker in content_type:
            boundary = content_type.split(marker, 1)[1].split(";", 1)[0]
            break
    else:
        return fields, images
    for part in body.split(b"--" + boundary.encode()):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        head, _, value = part.partition(b"\r\n\r\n")
        name = filename = ""
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if 'name="' in line:
                name = line.split('name="', 1)[1].split('"', 1)[0].removesuffix("[]")  # SDK 数组字段带 []
                if 'filename="' in line:
                    filename = line.split('filename="', 1)[1].split('"', 1)[0]
        if name == "image" and filename:
            images.append(filename)
        elif name:
            fields[name] = value.decode("utf-8", "replace")
    return fields, images


class Handler(BaseHTTPRequestHandler):
    server_version = "fake_painter/1.0"
    log_path: Path
    colour: tuple[int, int, int] = (128, 96, 64)
    calls = 0  # 应答计数：每次微扰颜色，使重绘产生新字节（take_fp 改变 → 真正重渲）

    def log_message(self, fmt, *args) -> None:  # 静默访问日志，避免刷屏
        pass

    def _reply(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        kind = "edits" if self.path.rstrip("/").endswith("/images/edits") else (
            "generations" if self.path.rstrip("/").endswith("/images/generations") else None)
        if kind is None:
            self._reply({"error": {"message": f"unsupported POST {self.path}"}}, 404)
            return
        if kind == "edits":
            fields, images = parse_multipart(body, self.headers.get("Content-Type", ""))
        else:
            try:
                fields, images = json.loads(body or b"{}"), []
            except ValueError:
                self._reply({"error": {"message": "invalid JSON"}}, 400)
                return
        with self.log_path.open("a", encoding="utf-8") as log:  # 用作发起 epaint/paint 的证据
            log.write(json.dumps({"t": time.time(), "endpoint": kind, "fields": fields,
                                  "images": images}, ensure_ascii=False) + "\n")
        Handler.calls += 1
        k = Handler.calls
        r, g, b = self.colour
        png = make_png(str(fields.get("size", "")), ((r + k) % 256, (g + 7 * k) % 256, (b + 13 * k) % 256))
        self._reply({"created": int(time.time()),
                     "data": [{"b64_json": png, "revised_prompt": fields.get("prompt", "")}]})

    def do_GET(self) -> None:
        self._reply({"error": {"message": f"unsupported GET {self.path}"}}, 404)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenAI 兼容 images API 假件")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--log", required=True, help="请求日志 jsonl 路径")
    parser.add_argument("--colour", default="128,96,64", help="应答 PNG 的 RGB，逗号分隔")
    args = parser.parse_args(argv)

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    Handler.log_path = Path(args.log)
    Handler.colour = tuple(int(x) for x in args.colour.split(","))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[fake_painter] listening on 127.0.0.1:{args.port}；日志：{args.log}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
