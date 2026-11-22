// 共享组件：阶段时间线 + 时间码格式化 + 产物图放大预览。

import { useEffect, useState } from "react";
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
  let done: boolean;
  if (typeof value === "boolean") done = value;
  else if (typeof value === "number") done = value > 0;
  else done = Boolean(value);
  // 审查依赖 take，进度数据异常时也不允许「渲染未开始、审查已绿」
  if (key === "review") return done && progress.takes > 0;
  return done;
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

// mm:ss.s，窗口 own_s / 源片 start 都用同一口径显示
export function formatClock(seconds: number): string {
  const safe = Math.max(0, seconds);
  const minutes = Math.floor(safe / 60);
  return `${String(minutes).padStart(2, "0")}:${(safe - minutes * 60).toFixed(1).padStart(4, "0")}`;
}

/** 缩略图点击放大：原图约 1920px 宽，网格里压到 ~200px 时帧号/标签完全不可读。 */
export function ZoomableImage({ src, alt, label }: { src: string; alt: string; label: string }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  return (
    <>
      <button type="button" className="thumb" onClick={() => setOpen(true)}
              aria-label={`放大 ${label}`} title="点击放大">
        <img src={src} alt={alt} loading="lazy" />
      </button>
      {open && (
        <div className="lightbox" role="dialog" aria-label={`${label} 原图`} onClick={() => setOpen(false)}>
          <div className="lightbox-bar" onClick={(event) => event.stopPropagation()}>
            <span>{label}</span>
            <a href={src} target="_blank" rel="noreferrer">查看原图</a>
            <button type="button" className="secondary" onClick={() => setOpen(false)}>关闭</button>
          </div>
          <img className="lightbox-img" src={src} alt={`${label} 原图`}
               onClick={(event) => event.stopPropagation()} />
        </div>
      )}
    </>
  );
}
