// useProgress（/ws/progress）与应用骨架测试：快照/事件合并、断线重连、合规标注。

import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { useProgress } from "../hooks";
import { makeProgress, makeTask, mockFetch } from "./helpers";
import { MockWebSocket, installMockWs } from "./mockWs";

function Probe() {
  const state = useProgress();
  return (
    <div>
      <span data-testid="conn">{String(state.connected)}</span>
      <span data-testid="count">{Object.keys(state.tasks).length}</span>
      <span data-testid="status">{state.tasks["t1"]?.status ?? "-"}</span>
      <span data-testid="final">{state.tasks["t1"]?.final ?? "-"}</span>
      <span data-testid="painted">{state.tasks["t1"]?.progress?.painted ?? "-"}</span>
      <span data-testid="logline">{(state.logs["t1"] ?? []).join("|")}</span>
    </div>
  );
}

describe("useProgress", () => {
  it("连接即收快照，其后合并 status/progress/log 事件", () => {
    installMockWs();
    render(<Probe />);
    const ws = MockWebSocket.instances[0];
    expect(ws.url).toContain("/ws/progress");
    act(() => ws.open());
    expect(screen.getByTestId("conn")).toHaveTextContent("true");

    act(() => ws.emit({ type: "snapshot", tasks: [makeTask()] }));
    expect(screen.getByTestId("count")).toHaveTextContent("1");
    expect(screen.getByTestId("status")).toHaveTextContent("running");

    act(() => ws.emit({ type: "task_status", task_id: "t1", status: "done", final: "/tmp/f.mp4" }));
    expect(screen.getByTestId("status")).toHaveTextContent("done");
    expect(screen.getByTestId("final")).toHaveTextContent("/tmp/f.mp4");

    act(() => ws.emit({ type: "progress", task_id: "t1", progress: makeProgress({ painted: 3 }) }));
    expect(screen.getByTestId("painted")).toHaveTextContent("3");

    act(() => ws.emit({ type: "log", task_id: "t1", line: "[pool] 交付" }));
    act(() => ws.emit({ type: "round_end", task_id: "t1", returncode: 0 }));
    expect(screen.getByTestId("logline")).toHaveTextContent("[pool] 交付");
    expect(screen.getByTestId("logline")).toHaveTextContent("rc=0");
  });

  it("未知任务的事件不创建条目", () => {
    installMockWs();
    render(<Probe />);
    const ws = MockWebSocket.instances[0];
    act(() => ws.open());
    act(() => ws.emit({ type: "task_status", task_id: "ghost", status: "done" }));
    expect(screen.getByTestId("count")).toHaveTextContent("0");
  });

  it("断线 2s 自动重连", () => {
    vi.useFakeTimers();
    try {
      installMockWs();
      render(<Probe />);
      const ws = MockWebSocket.instances[0];
      act(() => ws.open());
      act(() => ws.close());
      expect(screen.getByTestId("conn")).toHaveTextContent("false");
      act(() => vi.advanceTimersByTime(2000));
      expect(MockWebSocket.instances.length).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("App", () => {
  it("合规标注与导航、WS 状态徽标", async () => {
    installMockWs();
    mockFetch((url) => {
      if (url === "/api/tasks") return [];
      if (url === "/api/chars") return [];
      if (url === "/api/settings") {
        return { llm: { model: "m", base_url: "u", api_key_set: false },
                 painter: { model: "m", base_url: "u", api_key_set: false },
                 budget_default: 24, remake_home: "" };
      }
      throw new Error(`未 mock 的请求 ${url}`);
    });
    render(<App />);
    expect(screen.getByText("Powered by Viggle Remake · MiniMax H3")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "任务" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "角色库" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "设置" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("实时未连接");
    act(() => MockWebSocket.instances[0].open());
    expect(screen.getByRole("status")).toHaveTextContent("实时已连接");
    expect(await screen.findByText("暂无任务")).toBeInTheDocument();
  });
});