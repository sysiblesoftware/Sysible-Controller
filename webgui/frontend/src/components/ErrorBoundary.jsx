import React from "react";

// Top-level error boundary: a render-time throw anywhere in the tree is caught
// here and shown as a recoverable panel instead of white-screening the whole
// console. "Reload console" does a hard reload; "Try again" clears the error and
// re-renders (useful when the failure was a transient bad API shape).
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Keep a breadcrumb in the console for support; no telemetry is sent.
    // eslint-disable-next-line no-console
    console.error("Console render error:", error, info && info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    const msg = (this.state.error && this.state.error.message) || String(this.state.error);
    return (
      <div role="alert" style={{
        maxWidth: 560, margin: "12vh auto", padding: "28px 30px",
        background: "var(--panel, #1e2127)", border: "1px solid var(--border, #333)",
        borderRadius: 10, color: "var(--text, #e6e6e6)",
        fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
      }}>
        <h2 style={{ margin: "0 0 8px", fontSize: 20 }}>Something went wrong in the console</h2>
        <p style={{ margin: "0 0 16px", color: "var(--text-dim, #9aa0aa)", lineHeight: 1.55 }}>
          The page hit an unexpected error and stopped rendering. Your fleet and the
          controller are unaffected — this is only the web console view.
        </p>
        <pre style={{
          background: "var(--code-bg, #14161b)", color: "var(--text-dim, #9aa0aa)",
          padding: "10px 12px", borderRadius: 6, fontSize: 12.5, overflowX: "auto",
          margin: "0 0 18px", whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}>{msg}</pre>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button className="btn" onClick={() => window.location.reload()}>Reload console</button>
          <button className="btn ghost" onClick={() => this.setState({ error: null })}>Try again</button>
        </div>
      </div>
    );
  }
}
