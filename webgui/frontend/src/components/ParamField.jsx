import React from "react";

// Renders one action parameter from the /api/tools catalog: text / number /
// password / select / checkbox — the five types the registry uses.
export default function ParamField({ p, value, onChange }) {
  if (p.type === "checkbox") {
    return (
      <div className="checkrow">
        <input id={"p_" + p.name} type="checkbox" checked={Boolean(value)}
               onChange={(e) => onChange(p.name, e.target.checked)} />
        <label htmlFor={"p_" + p.name}>{p.label}</label>
      </div>
    );
  }
  return (
    <label className="field">
      <span>
        {p.label}{p.required ? " *" : ""}
        {p.help ? <span className="faint"> — {p.help}</span> : null}
      </span>
      {p.type === "select" ? (
        <select value={value ?? ""} onChange={(e) => onChange(p.name, e.target.value)}>
          {(p.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      ) : (
        <input
          type={p.type === "password" ? "password" : p.type === "number" ? "number" : "text"}
          value={value ?? ""}
          onChange={(e) => onChange(p.name, e.target.value)}
        />
      )}
    </label>
  );
}
