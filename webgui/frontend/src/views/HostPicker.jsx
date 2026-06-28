import React from "react";

// Checkbox grid for selecting one or more target hosts. `value` is an array
// of host ids; `onChange` gets the next array. `requireAgent` dims SSH-only
// hosts for actions that need an agent (informational — still selectable).
export default function HostPicker({ hosts, value, onChange }) {
  function toggle(id) {
    if (value.includes(id)) onChange(value.filter((x) => x !== id));
    else onChange([...value, id]);
  }
  const allSelected = hosts.length > 0 && value.length === hosts.length;
  return (
    <div className="targets-box">
      <div className="spread">
        <strong>Targets</strong>
        <button
          className="btn secondary sm"
          type="button"
          onClick={() => onChange(allSelected ? [] : hosts.map((h) => h.id))}
        >
          {allSelected ? "Clear all" : "Select all"}
        </button>
      </div>
      <div className="targets-grid">
        {hosts.map((h) => (
          <label className="target-chip" key={h.id}>
            <input
              type="checkbox"
              checked={value.includes(h.id)}
              onChange={() => toggle(h.id)}
            />
            <span>
              {h.label}{" "}
              {!h.has_agent && <span className="faint">(ssh)</span>}
            </span>
          </label>
        ))}
      </div>
    </div>
  );
}
