import React, { useState } from "react";
import { useAuth } from "./AuthContext.jsx";
import Login from "./pages/Login.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import ToolPage from "./pages/ToolPage.jsx";
import TerminalPage from "./pages/Terminal.jsx";
import FilesPage from "./pages/Files.jsx";
import EditionBadge from "./components/EditionBadge.jsx";

// Lightweight state-based router: the app only has two "screens"
// (dashboard grid, and a single tool view), so a full router dependency
// isn't worth it. `view` is either {name:"dashboard"} or
// {name:"tool", tool:"<tool name>"}.
export default function App() {
  const { user, loading, logout } = useAuth();
  const [view, setView] = useState({ name: "dashboard" });

  if (loading) {
    return <div className="center muted">Loading…</div>;
  }
  if (!user) {
    return <Login />;
  }

  const openTool = (tool) => {
    if (tool === "__terminal__") return setView({ name: "terminal" });
    if (tool === "__files__") return setView({ name: "files" });
    setView({ name: "tool", tool });
  };
  const goHome = () => setView({ name: "dashboard" });

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand" onClick={goHome} role="button" tabIndex={0}>
          <span className="brand-mark">◆</span>
          <span className="brand-name">Sysible Controller</span>
        </div>
        <div className="topbar-right">
          <EditionBadge />
          <span className="muted">{user}</span>
          <button className="btn-ghost" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      <main className="content">
        {view.name === "dashboard" && <Dashboard onOpenTool={openTool} />}
        {view.name === "tool" && (
          <ToolPage tool={view.tool} onBack={goHome} />
        )}
        {view.name === "terminal" && <TerminalPage onBack={goHome} />}
        {view.name === "files" && <FilesPage onBack={goHome} />}
      </main>
    </div>
  );
}
