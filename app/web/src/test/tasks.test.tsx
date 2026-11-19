// 页面 1 组件测试（mock API）：列表渲染、创建表单校验与 multipart 口径、swap 点选角色。

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TasksPage } from "../pages/TasksPage";
import { callOf, errorResponse, makeTask, mockFetch, renderPage } from "./helpers";

const SETTINGS = {
  llm: { model: "qwen3.7-plus", base_url: "https://llm.example/v1", api_key_set: true },
  painter: { model: "MiniMax-H3-Image", base_url: "https://paint.example/v1", api_key_set: false },
  budget_default: 24,
  remake_home: "",
};

const CHARS = [
  { name: "Alice", sheet: "Alice_sheet.png", photo: null, desc: "测试角色", outfit: "" },
];

function baseHandler(tasks = [makeTask()]) {
  return (url: string) => {
    if (url === "/api/tasks") return tasks;
    if (url === "/api/chars") return CHARS;
    if (url === "/api/settings") return SETTINGS;
    throw new Error(`未 mock 的请求 ${url}`);
  };
}

describe("TasksPage", () => {
  it("渲染任务列表：状态标签与成片下载入口", async () => {
    const done = makeTask({ task_id: "t2", description: "已完成任务", status: "done",
                            final: "/tmp/full.mp4", created_at: 2000 });
    mockFetch(baseHandler([makeTask(), done]));
    renderPage(<TasksPage />);
    expect(await screen.findByText("把涡轮机去掉")).toBeInTheDocument();
    expect(screen.getByText("已完成任务")).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    const download = screen.getByRole("link", { name: "下载成片" });
    expect(download).toHaveAttribute("href", "/api/tasks/t2/result");
    expect(screen.getByRole("link", { name: "把涡轮机去掉" }))
      .toHaveAttribute("href", "/tasks/t1");
  });

  it("空列表提示", async () => {
    mockFetch(baseHandler([]));
    renderPage(<TasksPage />);
    expect(await screen.findByText("暂无任务")).toBeInTheDocument();
  });

  it("校验：未选视频 / 描述为空", async () => {
    const user = userEvent.setup();
    mockFetch(baseHandler());
    renderPage(<TasksPage />);
    await screen.findByRole("heading", { name: "创建任务" });
    await user.click(screen.getByRole("button", { name: "创建任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请选择源视频");
    await user.upload(screen.getByLabelText(/源视频/), new File(["x"], "s.mp4", { type: "video/mp4" }));
    await user.click(screen.getByRole("button", { name: "创建任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请填写任务描述");
  });

  it("创建 edit 任务：multipart 字段口径与跳转", async () => {
    const user = userEvent.setup();
    const fetchFn = mockFetch((url, init) => {
      if (url === "/api/tasks" && init?.method === "POST") {
        return makeTask({ task_id: "tnew", status: "created" });
      }
      return baseHandler()(url);
    });
    renderPage(<TasksPage />);
    await screen.findByRole("heading", { name: "创建任务" });
    await user.upload(screen.getByLabelText(/源视频/), new File(["video"], "s.mp4", { type: "video/mp4" }));
    await user.type(screen.getByLabelText("任务描述"), "把涡轮机去掉");
    await user.clear(screen.getByLabelText(/预算上限/));
    await user.type(screen.getByLabelText(/预算上限/), "12");
    await user.type(screen.getByLabelText(/时长秒/), "5.5");
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    expect(await screen.findByTestId("detail-probe")).toBeInTheDocument();
    const call = callOf(fetchFn, "/api/tasks", "POST");
    expect(call).toBeTruthy();
    const form = call![1]!.body as FormData;
    expect(form.get("description")).toBe("把涡轮机去掉");
    expect(form.get("mode")).toBe("edit");
    expect(form.get("budget")).toBe("12");
    expect(form.get("start")).toBe("0");
    expect(form.get("dur")).toBe("5.5");
    expect(form.get("cast")).toBe("{}");
    expect((form.get("video") as File).name).toBe("s.mp4");
  });

  it("创建 swap 任务：cast 并入点选角色，参考图上传为 photo", async () => {
    const user = userEvent.setup();
    const fetchFn = mockFetch((url, init) => {
      if (url === "/api/tasks" && init?.method === "POST") {
        return makeTask({ task_id: "tswap", mode: "swap", status: "created" });
      }
      if (url === "/api/chars/Alice/photo" && init?.method === "POST") return CHARS[0];
      return baseHandler()(url);
    });
    renderPage(<TasksPage />);
    await screen.findByRole("heading", { name: "创建任务" });
    await user.selectOptions(screen.getByLabelText("模式"), "swap");
    await user.upload(screen.getByLabelText(/源视频/), new File(["v"], "s.mp4", { type: "video/mp4" }));
    await user.type(screen.getByLabelText("任务描述"), "换成 Alice");
    await user.type(screen.getByLabelText(/源人物 id/), "p0");
    await user.selectOptions(screen.getByLabelText(/目标角色/), "Alice");
    await user.upload(screen.getByLabelText(/参考图/), new File(["img"], "ref.png", { type: "image/png" }));
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    expect(await screen.findByTestId("detail-probe")).toBeInTheDocument();
    const form = callOf(fetchFn, "/api/tasks", "POST")![1]!.body as FormData;
    expect(form.get("mode")).toBe("swap");
    expect(JSON.parse(form.get("cast") as string)).toEqual({ p0: "Alice" });
    const photo = callOf(fetchFn, "/api/chars/Alice/photo", "POST");
    expect(photo).toBeTruthy();
    expect(((photo![1]!.body as FormData).get("file") as File).name).toBe("ref.png");
  });

  it("swap 未点选角色被拦截；后端错误展示 detail", async () => {
    const user = userEvent.setup();
    mockFetch((url, init) => {
      if (url === "/api/tasks" && init?.method === "POST") {
        return errorResponse(400, "swap 任务须给出 cast");
      }
      return baseHandler()(url);
    });
    renderPage(<TasksPage />);
    await screen.findByRole("heading", { name: "创建任务" });
    await user.selectOptions(screen.getByLabelText("模式"), "swap");
    await user.upload(screen.getByLabelText(/源视频/), new File(["v"], "s.mp4", { type: "video/mp4" }));
    await user.type(screen.getByLabelText("任务描述"), "换人");
    await user.click(screen.getByRole("button", { name: "创建任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("swap 任务须填写源人物 id");
  });

  it("WS 快照优先于初始列表", async () => {
    mockFetch(baseHandler([]));
    const live = makeTask({ task_id: "tlive", description: "实时任务", status: "running" });
    renderPage(<TasksPage />, { progress: { tasks: { tlive: live }, logs: {}, connected: true } });
    expect(await screen.findByText("实时任务")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("暂无任务")).not.toBeInTheDocument());
  });
});