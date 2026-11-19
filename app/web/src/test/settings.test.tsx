// 页面 4 设置组件测试（mock API）：掩码展示、保存口径、预算校验。

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SettingsPage } from "../pages/SettingsPage";
import { callOf, errorResponse, mockFetch, renderPage } from "./helpers";

const SETTINGS = {
  llm: { model: "qwen3.7-plus", base_url: "https://llm.example/v1", api_key_set: true },
  painter: { model: "MiniMax-H3-Image", base_url: "https://paint.example/v1", api_key_set: false },
  budget_default: 24,
  remake_home: "/data/viggle-remake",
};

function renderSettings() {
  return renderPage(<SettingsPage />, { route: "/settings", path: "/settings" });
}

describe("SettingsPage", () => {
  it("加载设置：密钥掩码、预算预填、路径只读", async () => {
    mockFetch((url) => {
      if (url === "/api/settings") return SETTINGS;
      throw new Error(`未 mock 的请求 ${url}`);
    });
    renderSettings();
    expect(await screen.findByDisplayValue("qwen3.7-plus")).toBeInTheDocument();
    expect(screen.getByDisplayValue("https://paint.example/v1")).toBeInTheDocument();
    expect(screen.getByLabelText(/API Key（已配置/)).toHaveValue("");
    expect(screen.getByLabelText(/API Key（未配置/)).toBeInTheDocument();
    expect(screen.getByLabelText(/预算默认值/)).toHaveValue(24);
    expect(screen.getByDisplayValue("/data/viggle-remake")).toHaveAttribute("readonly");
  });

  it("保存：PUT 三段口径，空密钥保留现场", async () => {
    const user = userEvent.setup();
    const fetchFn = mockFetch((url, init) => {
      if (url === "/api/settings" && init?.method === "PUT") {
        return { ...SETTINGS, llm: { ...SETTINGS.llm, model: "m2" }, budget_default: 16 };
      }
      if (url === "/api/settings") return SETTINGS;
      throw new Error(`未 mock 的请求 ${url}`);
    });
    renderSettings();
    await screen.findByDisplayValue("qwen3.7-plus");
    const model = screen.getByLabelText("模型", { selector: "#llm-model" });
    await user.clear(model);
    await user.type(model, "m2");
    const budget = screen.getByLabelText(/预算默认值/);
    await user.clear(budget);
    await user.type(budget, "16");
    await user.click(screen.getByRole("button", { name: "保存设置" }));
    expect(await screen.findByRole("status")).toHaveTextContent("设置已保存到 config.toml");
    const call = callOf(fetchFn, "/api/settings", "PUT");
    expect(JSON.parse(String(call![1]!.body))).toEqual({
      llm: { model: "m2", base_url: "https://llm.example/v1", api_key: "" },
      painter: { model: "MiniMax-H3-Image", base_url: "https://paint.example/v1", api_key: "" },
      budget: { max_images: 16 },
    });
  });

  it("预算非法被拦截；后端错误展示 detail", async () => {
    const user = userEvent.setup();
    mockFetch((url, init) => {
      if (url === "/api/settings" && init?.method === "PUT") {
        return errorResponse(400, "budget.max_images 应为正整数");
      }
      return SETTINGS;
    });
    renderSettings();
    await screen.findByDisplayValue("qwen3.7-plus");
    const budget = screen.getByLabelText(/预算默认值/);
    await user.clear(budget);
    await user.type(budget, "0");
    await user.click(screen.getByRole("button", { name: "保存设置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("预算默认值须为正整数");
  });
});