// 共享组件：阶段时间线（需求八页面 2：读片→规划→绘制→渲染→审查→拼片）。

import type { Progress } from "./types";

const STAGES: { key: keyof Progress; label: string }[] = [
  { key: "ingest", label: "读片" },
  { key: "plan", label: "规划" },
  { key: "painted", label: "绘制" },
  { key: "takes", label: "渲染" },
  { key: "review", label: "审查" },
  { key: "final", label: "拼片" },
];

export function stageDone(progress: Progress | undefined, key: keyof Progress): boolean {
  if (!progress) return false;
  const value = progress[key];
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value > 0;
  return Boolean(value);
}

export function StageTimeline({ progress }: { progress?: Progress }) {
  return (
    <ol className="timeline" aria-label="阶段时间线">
      {STAGES.map(({ key, label }) => {
        const done = stageDone(progress, key);
        const count = progress && typeof progress[key] === "number" ? (progress[key] as number) : 0;
        return (
          <li key={key} data-testid={`stage-${key}`} className={done ? "done" : "pending"}>
            <span className="dot" aria-hidden="true" />
            {label}
            {count > 0 && <span className="count">×{count}</span>}
          </li>
        );
      })}
    </ol>
  );
}