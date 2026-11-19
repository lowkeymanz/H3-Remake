// 页面 2/3 组件测试（mock API）：时间线、产物预览、成片、反馈重制、失败重试、日志。

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TaskDetailPage } from "../pages/TaskDetailPage";
import { callOf, makeProgress, makeTask, mockFetch, renderPage } from "./helpers";
import type { ProgressState } from "../hooks";
import type { Task } from "../types";

const ARTIFACTS = {
  keyframes: ["case/paint/g1/f60.png", "case/paint/g2/f120.png"],
  takes: ["case/takes/w0_480_s0_1.mp4"],
  strips: ["case/look/strips_0.jpg"],
  finals: ["case/final/full_480.mp4"],
};

function handler(task: Task, artifacts = ARTIFACTS) {
  return (url: string, init?: RequestInit) => {
    if (url === "/api/tasks/t1") return task;
    if (url === "/api/tasks/t1/artifacts") return artifacts;
    if (url === "/api/tasks/t1/log") return { task_id: "t1", log: "[pool] 领取任务" };
    if (url === "/api/tasks/t1/feedback" && init?.method === "POST") {
      const body = JSON.parse(String(init?.body)) as { text: string; windows: number[] };
      return { ...task, status: "created", rounds: task.rounds + 1,
               feedback: [...task.feedback, { text: body.text, windows: body.windows, at: 0 }],
               updated_at: task.updated_at + 10, prompt: "follow-up" };
    }
    throw new Error(`未 mock 的请求 ${url}`);
  };
}

function renderDetail(_task: Task, progress?: ProgressState) {
  return renderPage(<TaskDetailPage />, {
    route: "/tasks/t1",
    path: "/tasks/:taskId",
    ...(progress ? { progress } : {}),
  });
}

describe("TaskDetailPage", () => {
  it("阶段时间线按 progress 点亮，产物预览 URL 口径正确", async () => {
    const task = makeTask({ progress: makeProgress({ ingest: true, plan: true, painted: 2, takes: 1 }) });
    mockFetch(handler(task));
    renderDetail(task);
    expect(await screen.findByTestId("stage-ingest")).toHaveClass("done");
    expect(screen.getByTestId("stage-plan")).toHaveClass("done");
    expect(screen.getByTestId("stage-painted")).toHaveClass("done");
    expect(screen.getByTestId("stage-painted")).toHaveTextContent("×2");
    expect(screen.getByTestId("stage-takes")).toHaveClass("done");
    expect(screen.getByTestId("stage-review")).toHaveClass("pending");
    expect(screen.getByTestId("stage-final")).toHaveClass("pending");
    expect(screen.getByAltText("case/paint/g1/f60.png"))
      .toHaveAttribute("src", "/files/sessions/t1/case/paint/g1/f60.png");
    expect(screen.getByAltText("case/look/strips_0.jpg")).toBeInTheDocument();
    expect(screen.getByText("case/takes/w0_480_s0_1.mp4")).toBeInTheDocument();
    expect(screen.getByTestId("task-log")).toHaveTextContent("[pool] 领取任务");
  });

  it("终态展示成片播放与下载及反馈表单", async () => {
    const done = makeTask({ status: "done", final: "/tmp/full_480.mp4",
                            progress: makeProgress({ ingest: true, plan: true, painted: 2,
                                                     takes: 1, review: true, final: true, result: "done" }) });
    mockFetch(handler(done));
    renderDetail(done);
    expect(await screen.findByText("成片")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载成片" })).toHaveAttribute("href", "/api/tasks/t1/result");
    expect(screen.getByLabelText("反馈内容")).toBeInTheDocument();
  });

  it("running 不展示反馈表单与成片", async () => {
    const running = makeTask();
    mockFetch(handler(running));
    renderDetail(running);
    expect(await screen.findByText("阶段进度")).toBeInTheDocument();
    expect(screen.queryByLabelText("反馈内容")).not.toBeInTheDocument();
    expect(screen.queryByText("成片")).not.toBeInTheDocument();
  });

  it("反馈重制：点名窗口解析与提交口径", async () => {
    const user = userEvent.setup();
    const done = makeTask({ status: "done", final: "/tmp/full.mp4" });
    const fetchFn = mockFetch(handler(done));
    renderDetail(done);
    await screen.findByLabelText("反馈内容");
    await user.type(screen.getByLabelText("反馈内容"), "第 2 窗脸部不像");
    await user.type(screen.getByLabelText(/点名窗口/), "0,2");
    await user.click(screen.getByRole("button", { name: "提交反馈" }));
    expect(await screen.findByRole("status")).toHaveTextContent("反馈已提交");
    const call = callOf(fetchFn, "/api/tasks/t1/feedback", "POST");
    expect(JSON.parse(String(call![1]!.body))).toEqual({ text: "第 2 窗脸部不像", windows: [0, 2] });
  });

  it("反馈校验：空内容与非法窗口", async () => {
    const user = userEvent.setup();
    const done = makeTask({ status: "done", final: "/tmp/full.mp4" });
    mockFetch(handler(done));
    renderDetail(done);
    await screen.findByLabelText("反馈内容");
    await user.click(screen.getByRole("button", { name: "提交反馈" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("反馈内容不能为空");
    await user.type(screen.getByLabelText("反馈内容"), "重做");
    await user.type(screen.getByLabelText(/点名窗口/), "a");
    await user.click(screen.getByRole("button", { name: "提交反馈" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("窗口须为非负整数");
  });

  it("失败任务展示失败重试按钮并下发重试反馈", async () => {
    const user = userEvent.setup();
    const failed = makeTask({ status: "failed" });
    const fetchFn = mockFetch(handler(failed));
    renderDetail(failed);
    const retry = await screen.findByRole("button", { name: "失败重试" });
    await user.click(retry);
    expect(await screen.findByRole("status")).toHaveTextContent("反馈已提交");
    const call = callOf(fetchFn, "/api/tasks/t1/feedback", "POST");
    expect(JSON.parse(String(call![1]!.body))).toEqual({ text: "重试失败任务", windows: [] });
  });

  it("WS 实时日志并入日志视图", async () => {
    const task = makeTask();
    mockFetch(handler(task));
    renderDetail(task, { tasks: {}, logs: { t1: ["[watchdog] 第 1 轮未交付"] }, connected: true });
    expect(await screen.findByTestId("task-log"))
      .toHaveTextContent("[watchdog] 第 1 轮未交付");
  });

  it("WS 快照任务优先展示（含最新状态）", async () => {
    const base = makeTask({ status: "running" });
    const live = makeTask({ status: "done", final: "/tmp/full.mp4" });
    mockFetch(handler(base));
    renderDetail(base, { tasks: { t1: live }, logs: {}, connected: true });
    expect(await screen.findByText("已完成")).toBeInTheDocument();
  });

  it("反馈提交后历史即时可见（不被旧 WS 快照盖掉）", async () => {
    const user = userEvent.setup();
    const done = makeTask({ status: "done", final: "/tmp/full.mp4", updated_at: 1000 });
    const stale = makeTask({ status: "done", final: "/tmp/full.mp4", updated_at: 1000 });
    mockFetch(handler(done));
    renderDetail(done, { tasks: { t1: stale }, logs: {}, connected: true });
    await screen.findByLabelText("反馈内容");
    await user.type(screen.getByLabelText("反馈内容"), "第 0 窗色调偏暗");
    await user.type(screen.getByLabelText(/点名窗口/), "0");
    await user.click(screen.getByRole("button", { name: "提交反馈" }));
    expect(await screen.findByRole("status")).toHaveTextContent("反馈已提交");
    // 反馈响应 updated_at 更新（真实 store 口径），胜过旧快照 → 历史即时展示
    expect(await screen.findByText(/第 1 次反馈：第 0 窗色调偏暗/)).toBeInTheDocument();
  });
});