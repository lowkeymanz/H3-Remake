// 测试基建：mock fetch 路由、任务工厂、带 Router/进度 Context 的渲染器。

import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { ProgressContext } from "../hooks";
import type { ProgressState } from "../hooks";
import type { Progress, Task } from "../types";

export type FetchHandler = (url: string, init?: RequestInit) => unknown;

export function mockFetch(handler: FetchHandler) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const result = await handler(String(input), init);
    if (result instanceof Response) return result;
    return new Response(JSON.stringify(result ?? null), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

export function errorResponse(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function makeProgress(overrides: Partial<Progress> = {}): Progress {
  return { ingest: false, plan: false, painted: 0, takes: 0, review: false, final: false,
           result: null, ...overrides };
}

export function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    task_id: "t1", mode: "edit", description: "把涡轮机去掉", cast: {}, budget: 24,
    start: 0, dur: null, src: "/tmp/source.mp4", status: "running", rounds: 1,
    feedback: [], final: "", created_at: 1000, updated_at: 1000,
    progress: makeProgress(), ...overrides,
  };
}

export const EMPTY_PROGRESS: ProgressState = { tasks: {}, logs: {}, connected: false };

export function renderPage(ui: ReactElement, opts: {
  route?: string;
  path?: string;
  progress?: ProgressState;
} = {}) {
  const { route = "/", path = "/", progress = EMPTY_PROGRESS } = opts;
  return render(
    <ProgressContext.Provider value={progress}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
          <Route path="/tasks/:taskId" element={
            <div data-testid="detail-probe">
              详情页
              <Link to={path} data-testid="back-link">返回</Link>
            </div>
          } />
        </Routes>
      </MemoryRouter>
    </ProgressContext.Provider>,
  );
}

// 从 mock fetch 调用中找指定 url + method 的请求
export function callOf(fn: ReturnType<typeof mockFetch>, url: string, method = "GET") {
  return fn.mock.calls.find(([input, init]) =>
    String(input) === url && ((init?.method ?? "GET") === method));
}