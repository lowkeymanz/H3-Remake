// REST 客户端：与 app/server/api.py 端点一一对应；错误统一抛 ApiError（带后端 detail）。

import type { Artifacts, CharEntry, Settings, Task } from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* 非 JSON 错误体保留状态码口径 */
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export const api = {
  listTasks: () => request<Task[]>("/api/tasks"),
  getTask: (id: string) => request<Task>(`/api/tasks/${id}`),
  createTask: (form: FormData) =>
    request<Task>("/api/tasks", { method: "POST", body: form }),
  artifacts: (id: string) => request<Artifacts>(`/api/tasks/${id}/artifacts`),
  log: (id: string) => request<{ task_id: string; log: string }>(`/api/tasks/${id}/log`),
  feedback: (id: string, text: string, windows: number[]) =>
    request<Task & { prompt: string }>(`/api/tasks/${id}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, windows }),
    }),
  resultUrl: (id: string) => `/api/tasks/${id}/result`,
  fileUrl: (id: string, rel: string) => `/files/sessions/${id}/${rel}`,

  chars: () => request<CharEntry[]>("/api/chars"),
  uploadSheet: (name: string, file: File, desc: string, outfit: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("desc", desc);
    form.append("outfit", outfit);
    return request<CharEntry>(`/api/chars/${encodeURIComponent(name)}/sheet`, {
      method: "POST",
      body: form,
    });
  },
  uploadPhoto: (name: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<CharEntry>(`/api/chars/${encodeURIComponent(name)}/photo`, {
      method: "POST",
      body: form,
    });
  },
  deleteChar: (name: string) =>
    request<{ deleted: string }>(`/api/chars/${encodeURIComponent(name)}`, { method: "DELETE" }),
  charFileUrl: (filename: string) => `/files/chars/${encodeURIComponent(filename)}`,

  settings: () => request<Settings>("/api/settings"),
  saveSettings: (body: Record<string, unknown>) =>
    request<Settings>("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};