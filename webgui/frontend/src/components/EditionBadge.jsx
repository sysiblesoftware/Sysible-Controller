import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Mirrors the desktop "Community Edition · N/10 hosts" badge. Shows the
// edition by default; only an explicit unlimited signal (host_limit ===
// null from the backend) hides it.
export default function EditionBadge() {
  const [info, setInfo] = useState(undefined);

  useEffect(() => {
    api.edition().then(setInfo).catch(() => setInfo({}));
  }, []);

  if (info === undefined) return null;
  if ("host_limit" in info && info.host_limit === null) return null;

  let text = "Community Edition";
  const { host_limit: limit, host_count: count } = info || {};
  if (Number.isInteger(limit) && Number.isInteger(count)) {
    text += ` · ${count}/${limit} hosts`;
  } else if (Number.isInteger(limit)) {
    text += ` · up to ${limit} hosts`;
  }
  return <span className="edition-badge" title="Community edition">{text}</span>;
}
