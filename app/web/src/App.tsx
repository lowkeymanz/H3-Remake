// 应用骨架：路由四页面 + 全局 WS 进度 Context + 合规标注（MiniMax H3 Community License）。

import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { ProgressContext, useProgress } from "./hooks";
import { TasksPage } from "./pages/TasksPage";
import { TaskDetailPage } from "./pages/TaskDetailPage";
import { CharsPage } from "./pages/CharsPage";
import { SettingsPage } from "./pages/SettingsPage";

export function App() {
  const progress = useProgress();
  return (
    <ProgressContext.Provider value={progress}>
      <BrowserRouter>
        <div className="layout">
          <header className="topbar">
            <span className="brand">Viggle Remake</span>
            <nav>
              <NavLink to="/" end>任务</NavLink>
              <NavLink to="/chars">角色库</NavLink>
              <NavLink to="/settings">设置</NavLink>
            </nav>
            <span className={progress.connected ? "ws on" : "ws off"} role="status">
              {progress.connected ? "实时已连接" : "实时未连接"}
            </span>
          </header>
          <main>
            <Routes>
              <Route path="/" element={<TasksPage />} />
              <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
              <Route path="/chars" element={<CharsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Routes>
          </main>
          <footer className="compliance">Powered by Viggle Remake · MiniMax H3</footer>
        </div>
      </BrowserRouter>
    </ProgressContext.Provider>
  );
}