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
  { name: "Bob", sheet: "Bob_sheet.png", photo: null, desc: "测试角色2", outfit: "" },
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
  it("渲染历史任务卡片：状态标签、创建时间、成片下载与整行跳转", async () => {
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
    expect(screen.getByRole("link", { name: "查看任务：把涡轮机去掉" }))
      .toHaveAttribute("href", "/tasks/t1");
    expect(screen.getByRole("link", { name: "查看任务：已完成任务" }))
      .toHaveAttribute("href", "/tasks/t2");
    // created_at=1000/2000 → 两张卡片均显示"01-01 HH:MM"（时区无关：只验证月份-日期）
    expect(screen.getAllByText(/01-01 \d{2}:\d{2}/).length).toBe(2);
  });

  it("历史任务卡片点击跳转详情页", async () => {
    const user = userEvent.setup();
    mockFetch(baseHandler());
    renderPage(<TasksPage />);
    await screen.findByRole("link", { name: "查看任务：把涡轮机去掉" });
    await user.click(screen.getByRole("link", { name: "查看任务：把涡轮机去掉" }));
    expect(await screen.findByTestId("detail-probe")).toBeInTheDocument();
  });

  it("表单防污染：填一半表单 → 看历史任务 → 返回，表单必须全新清空", async () => {
    const user = userEvent.setup();
    const fetchFn = mockFetch(baseHandler());
    renderPage(<TasksPage />);
    await screen.findByRole("heading", { name: "创建任务" });
    // 填一半表单（含 multipart 提交路径用到的字段）
    await user.upload(screen.getByLabelText(/源视频/), new File(["x"], "s.mp4", { type: "video/mp4" }));
    await user.type(screen.getByLabelText("任务描述"), "写了一半的描述");
    await user.type(screen.getByLabelText(/预算上限/), "9");
    // 中途去查看历史任务
    await user.click(screen.getByRole("link", { name: "查看任务：把涡轮机去掉" }));
    expect(await screen.findByTestId("detail-probe")).toBeInTheDocument();
    // 返回任务页：组件重新挂载，表单必须全空（描述/预算/视频均不残留）
    await user.click(screen.getByTestId("back-link"));
    expect(await screen.findByRole("heading", { name: "创建任务" })).toBeInTheDocument();
    expect(screen.getByLabelText("任务描述")).toHaveValue("");
    await screen.findByDisplayValue(String(SETTINGS.budget_default));  // 预算回落默认值
    expect(screen.queryByText("写了一半的描述")).not.toBeInTheDocument();
    // 此时创建任务：校验应报"请选择源视频"（视频不残留），且无 POST 发出
    await user.click(screen.getByRole("button", { name: "创建任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请选择源视频");
    expect(callOf(fetchFn, "/api/tasks", "POST")).toBeUndefined();
  });

  it("WS 快照与 REST 列表合并：同任务取 updated_at 新者、新任务不丢", async () => {
    // REST 拉到 t1（updated_at 旧）；WS 快照里 t1 已是 done（updated_at 新）且多了 t3
    const wsTask = makeTask({ task_id: "t1", status: "done", updated_at: 5000 });
    const wsNew = makeTask({ task_id: "t3", description: "WS 新任务", status: "done",
                             updated_at: 4000, created_at: 3000 });
    mockFetch(baseHandler());
    renderPage(<TasksPage />, { progress: { tasks: { t1: wsTask, t3: wsNew }, logs: {}, connected: true } });
    expect(await screen.findByText("WS 新任务")).toBeInTheDocument();
    // 两个卡片均为"已完成"（t1 取 WS 新状态而非 REST 的 running）
    expect(screen.getAllByText("已完成").length).toBe(2);
    expect(screen.queryByText("运行中")).not.toBeInTheDocument();
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

  it("swap 多人映射：动态增行、cast 多组、逐行参考图上传为 photo", async () => {
    const user = userEvent.setup();
    const fetchFn = mockFetch((url, init) => {
      if (url === "/api/tasks" && init?.method === "POST") {
        return makeTask({ task_id: "tmulti", mode: "swap", status: "created" });
      }
      if (url === "/api/chars/Bob/photo" && init?.method === "POST") return CHARS[1];
      return baseHandler()(url);
    });
    renderPage(<TasksPage />);
    await screen.findByRole("heading", { name: "创建任务" });
    await user.selectOptions(screen.getByLabelText("模式"), "swap");
    await user.upload(screen.getByLabelText(/源视频/), new File(["v"], "s.mp4", { type: "video/mp4" }));
    await user.type(screen.getByLabelText("任务描述"), "两人都换");
    await user.type(screen.getByLabelText(/源人物 id/), "p0");
    await user.selectOptions(screen.getByLabelText(/目标角色/), "Alice");
    await user.click(screen.getByRole("button", { name: "添加人物映射" }));
    const ids = screen.getAllByLabelText(/源人物 id/);
    const chars = screen.getAllByLabelText(/目标角色/);
    expect(ids).toHaveLength(2);
    await user.type(ids[1], "p1");
    await user.selectOptions(chars[1], "Bob");
    await user.upload(screen.getAllByLabelText(/参考图/)[1], new File(["img"], "bob.png", { type: "image/png" }));
    await user.click(screen.getByRole("button", { name: "创建任务" }));

    expect(await screen.findByTestId("detail-probe")).toBeInTheDocument();
    const form = callOf(fetchFn, "/api/tasks", "POST")![1]!.body as FormData;
    expect(JSON.parse(form.get("cast") as string)).toEqual({ p0: "Alice", p1: "Bob" });
    const photo = callOf(fetchFn, "/api/chars/Bob/photo", "POST");
    expect(photo).toBeTruthy();
    expect(((photo![1]!.body as FormData).get("file") as File).name).toBe("bob.png");
    expect(callOf(fetchFn, "/api/chars/Alice/photo", "POST")).toBeFalsy();
  });

  it("swap 多人校验：源人物 id 重复 / 增行未填全 均被拦截", async () => {
    const user = userEvent.setup();
    mockFetch(baseHandler());
    renderPage(<TasksPage />);
    await screen.findByRole("heading", { name: "创建任务" });
    await user.selectOptions(screen.getByLabelText("模式"), "swap");
    await user.upload(screen.getByLabelText(/源视频/), new File(["v"], "s.mp4", { type: "video/mp4" }));
    await user.type(screen.getByLabelText("任务描述"), "换人");
    await user.type(screen.getByLabelText(/源人物 id/), "p0");
    await user.selectOptions(screen.getByLabelText(/目标角色/), "Alice");
    await user.click(screen.getByRole("button", { name: "添加人物映射" }));
    // 第二行未填 → 拦截
    await user.click(screen.getByRole("button", { name: "创建任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("每行均须填全");
    // 第二行填重复 id → 拦截
    await user.type(screen.getAllByLabelText(/源人物 id/)[1], "p0");
    await user.selectOptions(screen.getAllByLabelText(/目标角色/)[1], "Bob");
    await user.click(screen.getByRole("button", { name: "创建任务" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("源人物 id 不能重复");
  });

  it("swap 移除行：只剩一行时移除按钮禁用", async () => {
    const user = userEvent.setup();
    mockFetch(baseHandler());
    renderPage(<TasksPage />);
    await screen.findByRole("heading", { name: "创建任务" });
    await user.selectOptions(screen.getByLabelText("模式"), "swap");
    expect(screen.getByRole("button", { name: "移除" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "添加人物映射" }));
    expect(screen.getAllByLabelText(/源人物 id/)).toHaveLength(2);
    const removes = screen.getAllByRole("button", { name: "移除" });
    expect(removes[0]).not.toBeDisabled();
    await user.click(removes[1]);
    expect(screen.getAllByLabelText(/源人物 id/)).toHaveLength(1);
    expect(screen.getByRole("button", { name: "移除" })).toBeDisabled();
  });

  it("WS 快照优先于初始列表", async () => {
    mockFetch(baseHandler([]));
    const live = makeTask({ task_id: "tlive", description: "实时任务", status: "running" });
    renderPage(<TasksPage />, { progress: { tasks: { tlive: live }, logs: {}, connected: true } });
    expect(await screen.findByText("实时任务")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("暂无任务")).not.toBeInTheDocument());
  });
});