// 页面 4 角色库组件测试（mock API）：浏览、上传 sheet、补充 photo、删除。

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { CharsPage } from "../pages/CharsPage";
import { callOf, mockFetch, renderPage } from "./helpers";

const ALICE = { name: "Alice", sheet: "Alice_sheet.png", photo: "Alice_photo.png",
                desc: "测试角色", outfit: "红裙" };

describe("CharsPage", () => {
  it("浏览角色：sheet/photo 预览与描述", async () => {
    mockFetch((url) => {
      if (url === "/api/chars") return [ALICE];
      throw new Error(`未 mock 的请求 ${url}`);
    });
    renderPage(<CharsPage />, { route: "/chars", path: "/chars" });
    expect(await screen.findByTestId("char-Alice")).toBeInTheDocument();
    expect(screen.getByAltText("Alice sheet")).toHaveAttribute("src", "/files/chars/Alice_sheet.png");
    expect(screen.getByAltText("Alice photo")).toHaveAttribute("src", "/files/chars/Alice_photo.png");
    expect(screen.getByText("测试角色")).toBeInTheDocument();
    expect(screen.getByText("服装：红裙")).toBeInTheDocument();
  });

  it("空库提示", async () => {
    mockFetch(() => []);
    renderPage(<CharsPage />, { route: "/chars", path: "/chars" });
    expect(await screen.findByText("角色库为空")).toBeInTheDocument();
  });

  it("上传 sheet：multipart 口径与入库刷新", async () => {
    const user = userEvent.setup();
    const BOB = { ...ALICE, name: "Bob", sheet: "Bob_sheet.png", photo: null };
    let listed = false;
    const fetchFn = mockFetch((url, init) => {
      if (url === "/api/chars") return listed ? [BOB] : [];
      if (url === "/api/chars/Bob/sheet" && init?.method === "POST") {
        listed = true;
        return BOB;
      }
      throw new Error(`未 mock 的请求 ${url}`);
    });
    renderPage(<CharsPage />, { route: "/chars", path: "/chars" });
    await screen.findByText("角色库为空");
    await user.type(screen.getByLabelText("角色名"), "Bob");
    await user.upload(screen.getByLabelText(/角色 sheet/), new File(["img"], "sheet.png", { type: "image/png" }));
    await user.type(screen.getByLabelText(/角色描述/), "新角色");
    await user.click(screen.getByRole("button", { name: "上传角色" }));
    expect(await screen.findByRole("status")).toHaveTextContent("角色 Bob 已入库");
    const call = callOf(fetchFn, "/api/chars/Bob/sheet", "POST");
    const form = call![1]!.body as FormData;
    expect((form.get("file") as File).name).toBe("sheet.png");
    expect(form.get("desc")).toBe("新角色");
    expect(form.get("outfit")).toBe("");
    await waitFor(() => expect(screen.getByTestId("char-Bob")).toBeInTheDocument());
  });

  it("校验：缺角色名 / 缺 sheet", async () => {
    const user = userEvent.setup();
    mockFetch(() => []);
    renderPage(<CharsPage />, { route: "/chars", path: "/chars" });
    await screen.findByText("角色库为空");
    await user.click(screen.getByRole("button", { name: "上传角色" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请填写角色名");
    await user.type(screen.getByLabelText("角色名"), "Bob");
    await user.click(screen.getByRole("button", { name: "上传角色" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("请选择角色 sheet 图片");
  });

  it("补充 photo 与删除角色", async () => {
    const user = userEvent.setup();
    let deleted = false;
    const fetchFn = mockFetch((url, init) => {
      if (url === "/api/chars") return deleted ? [] : [ALICE];
      if (url === "/api/chars/Alice/photo" && init?.method === "POST") return ALICE;
      if (url === "/api/chars/Alice" && init?.method === "DELETE") {
        deleted = true;
        return { deleted: "Alice" };
      }
      throw new Error(`未 mock 的请求 ${url}`);
    });
    renderPage(<CharsPage />, { route: "/chars", path: "/chars" });
    await screen.findByTestId("char-Alice");
    await user.upload(screen.getByLabelText("补充 photo"), new File(["p"], "p.png", { type: "image/png" }));
    await waitFor(() => expect(callOf(fetchFn, "/api/chars/Alice/photo", "POST")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "删除" }));
    expect(await screen.findByRole("status")).toHaveTextContent("角色 Alice 已删除");
    await waitFor(() => expect(screen.queryByTestId("char-Alice")).not.toBeInTheDocument());
    expect(callOf(fetchFn, "/api/chars/Alice", "DELETE")).toBeTruthy();
  });
});