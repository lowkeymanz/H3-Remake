// /ws/progress 接入（M5.2 事件总线）：连接即收快照，其后实时合并事件；断线 2s 自动重连。

import { createContext, useContext, useEffect, useState } from "react";
import type { Task, WsEvent } from "./types";

export interface ProgressState {
  tasks: Record<string, Task>;
  logs: Record<string, string[]>;
  connected: boolean;
}

// App 层单 WS 连接，页面经 Context 消费（避免每页各开一条）
export const ProgressContext = createContext<ProgressState>({ tasks: {}, logs: {}, connected: false });
export const useProgressState = () => useContext(ProgressContext);

const LOG_CAP = 500;

export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${path}`;
}

function applyEvent(state: ProgressState, event: WsEvent): ProgressState {
  if (event.type === "snapshot") {
    const tasks: Record<string, Task> = {};
    for (const task of event.tasks) tasks[task.task_id] = task;
    return { ...state, tasks };
  }
  const task = state.tasks[event.task_id];
  if (event.type === "task_status") {
    if (!task) return state;
    const merged: Task = { ...task, status: event.status };
    if (event.rounds !== undefined) merged.rounds = event.rounds;
    if (event.final !== undefined) merged.final = event.final;
    return { ...state, tasks: { ...state.tasks, [event.task_id]: merged } };
  }
  if (event.type === "progress") {
    if (!task) return state;
    return { ...state, tasks: { ...state.tasks, [event.task_id]: { ...task, progress: event.progress } } };
  }
  if (event.type === "log" || event.type === "round_start" || event.type === "round_end") {
    const line = event.type === "log" ? event.line
      : event.type === "round_start" ? `[round] 驱动开始（提示 ${event.prompt_chars} 字符）`
      : `[round] 驱动结束 rc=${event.returncode}`;
    const lines = [...(state.logs[event.task_id] ?? []), line].slice(-LOG_CAP);
    return { ...state, logs: { ...state.logs, [event.task_id]: lines } };
  }
  return state;
}

export function useProgress(path = "/ws/progress"): ProgressState {
  const [state, setState] = useState<ProgressState>({ tasks: {}, logs: {}, connected: false });

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const connect = () => {
      socket = new WebSocket(wsUrl(path));
      socket.onopen = () => setState((prev) => ({ ...prev, connected: true }));
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(String(message.data)) as WsEvent;
          setState((prev) => applyEvent(prev, event));
        } catch {
          /* 非法帧丢弃 */
        }
      };
      socket.onclose = () => {
        setState((prev) => ({ ...prev, connected: false }));
        if (!closed) retry = setTimeout(connect, 2000);
      };
    };
    connect();

    return () => {
      closed = true;
      if (retry !== null) clearTimeout(retry);
      socket?.close();
    };
  }, [path]);

  return state;
}