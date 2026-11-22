// 与后端 app/server 的 JSON 口径逐字对齐（store.Task.to_dict / prompts.session_progress /
// chars.list_chars / settings.read_settings / pool 事件）。

export type TaskStatus = "created" | "running" | "done" | "partial" | "failed" | "interrupted";
export type Mode = "swap" | "edit";

export interface Progress {
  ingest: boolean;
  plan: boolean;
  painted: number;
  takes: number;
  review: boolean;
  final: boolean;
  result: string | null;
}

export interface FeedbackEntry {
  text: string;
  windows: number[];
  at: number;
}

export interface Task {
  task_id: string;
  mode: Mode;
  description: string;
  cast: Record<string, string>;
  budget: number | null;
  start: number;
  dur: number | null;
  src: string;
  status: TaskStatus;
  rounds: number;
  feedback: FeedbackEntry[];
  final: string;
  created_at: number;
  updated_at: number;
  progress?: Progress;
}

// /api/tasks/{id}/artifacts 的窗口清单：refs.json 帧区间 + 交付报告（帧号按 24fps 换算秒）
export interface WindowInfo {
  index: number;
  own: [number, number];
  render: [number, number];
  own_s: [number, number];
  passthrough: boolean;
  picked_seed: number | null;
  grade: string | null;
  note: string | null;
}

export interface Artifacts {
  keyframes: string[];
  takes: string[];
  strips: string[];
  finals: string[];
  windows: WindowInfo[];
}

export interface CharEntry {
  name: string;
  sheet: string;
  photo: string | null;
  desc: string;
  outfit: string;
}

export interface ApiSection {
  model: string;
  base_url: string;
  api_key_set: boolean;
}

export interface Settings {
  llm: ApiSection;
  painter: ApiSection;
  budget_default: number;
  remake_home: string;
}

// /ws/progress 事件（pool.py 口径）
export type WsEvent =
  | { type: "snapshot"; tasks: Task[] }
  | { type: "task_status"; task_id: string; status: TaskStatus; rounds?: number;
      final?: string; reason?: string; watchdog_rounds?: number }
  | { type: "round_start"; task_id: string; prompt_chars: number }
  | { type: "round_end"; task_id: string; returncode: number }
  | { type: "log"; task_id: string; line: string }
  | { type: "progress"; task_id: string; progress: Progress };

export const TERMINAL: TaskStatus[] = ["done", "partial", "failed"];

export const STATUS_LABEL: Record<TaskStatus, string> = {
  created: "排队中",
  running: "运行中",
  done: "已完成",
  partial: "部分交付",
  failed: "失败",
  interrupted: "已中断",
};