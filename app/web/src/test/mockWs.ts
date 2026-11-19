// mock WebSocket：记录实例，测试侧手动 open/emit/close 驱动 useProgress。

import { vi } from "vitest";

export class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }

  send(_data: string): void {
    /* 前端只收不发 */
  }

  close(): void {
    this.readyState = 3;
    this.onclose?.();
  }

  // ── 测试驱动 ──
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  emit(event: unknown): void {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}

export function installMockWs(): void {
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
}