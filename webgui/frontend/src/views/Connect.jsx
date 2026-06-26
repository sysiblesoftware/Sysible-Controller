import React, { useState } from "react";
import Terminal from "./Terminal.jsx";
import Files from "./Files.jsx";

// Sysible Connect: terminals + file transfer under one section, matching the
// desktop's combined Connect window.
export default function Connect() {
  const [tab, setTab] = useState("terminals");
  return (
    <div>
      <div className="tabs" style={{ marginBottom: 16 }}>
        <button className={tab === "terminals" ? "active" : ""} onClick={() => setTab("terminals")}>
          Terminals
        </button>
        <button className={tab === "files" ? "active" : ""} onClick={() => setTab("files")}>
          File Transfer
        </button>
      </div>
      {tab === "terminals" ? <Terminal /> : <Files />}
    </div>
  );
}
