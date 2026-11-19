"""假 agent（M5.2）：进程池 E2E 的子进程假件——零 GPU / 零 API 费 / 不依赖 Hermes 与 MCP。

按 --plan 的 JSON 步骤列表模拟一轮 agent 行为（每步打印日志供进程池采集）：
- {"sleep": 秒}                      耗时（模拟驱动过程，供 ticker 进度事件观测）；
- {"touch": "case/meta.json"}        相对会话目录落空文件（模拟阶段产物）；
- {"final": "case/final/full_480.mp4"} 落成片（固定字节，供下载断言）；
- {"result": {...}}                  写 result.json（交付标记，看门狗据此停轮）；
- {"exit": 码}                       以指定码退出（模拟半途收工/被 kill 的非零退出）。

提示原文由驱动方写入 --prompt-file（测试据此断言首轮/恢复/反馈提示口径）。
与 fake_llm/fake_painter/fake_comfy_worker 同纪律：只替换"贵"的部分，协议口径不变。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="假 agent：按计划模拟一轮会话驱动")
    parser.add_argument("--home", required=True, help="REMAKE_HOME（会话目录根）")
    parser.add_argument("--sid", required=True, help="会话 id（= task_id）")
    parser.add_argument("--prompt-file", required=True, help="驱动提示原文（只读打印首行）")
    parser.add_argument("--plan", required=True, help="步骤列表 JSON 文件")
    args = parser.parse_args(argv)

    session = Path(args.home) / "sessions" / args.sid
    session.mkdir(parents=True, exist_ok=True)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    print(f"[fake-agent] sid={args.sid} prompt: {prompt.splitlines()[0]}", flush=True)
    steps = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    last_final = None
    for step in steps:
        if "sleep" in step:
            print(f"[fake-agent] sleep {step['sleep']}s", flush=True)
            time.sleep(float(step["sleep"]))
        elif "touch" in step:
            path = session / step["touch"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
            print(f"[fake-agent] touch {step['touch']}", flush=True)
        elif "final" in step:
            path = session / step["final"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fake-final-video")
            last_final = path
            print(f"[fake-agent] final {step['final']}", flush=True)
        elif "result" in step:
            result = dict(step["result"])
            if not result.get("final") and last_final is not None:
                result["final"] = str(last_final)  # 同 save_result 口径：final 给绝对路径
            (session / "result.json").write_text(json.dumps(result, ensure_ascii=False),
                                                 encoding="utf-8")
            print(f"[fake-agent] result status={result.get('status')}", flush=True)
        elif "exit" in step:
            print(f"[fake-agent] exit {step['exit']}", flush=True)
            return int(step["exit"])
        else:
            print(f"[fake-agent] 未知步骤 {step!r}", file=sys.stderr, flush=True)
            return 2
    print("[fake-agent] round done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())