# Sysible Controller — UX / polish backlog

Small issues to fix later, captured during lab testing. Newest first.
Each item: symptom → root cause (where known) → suggested fix.

---

## 1. Firewall tool has a stray tab literally named "Firewall Administration"

**Symptom:** Inside the Firewall tool the tab row is
`Firewalld · Ports · Zones · Rich Rules · ufw · nftables · iptables · Firewall
Administration`. That last tab has the same name as the tool itself and holds
only a single action, "Nft: save (persist across reboot)". Confusing — looks
like a leftover.

**Root cause:** `views/ToolPage.jsx` derives a tab as `a.tab || tool.tool`. Any
firewall action registered in `webgui/actions.py` (tool `"Firewall
Administration"`) **without an explicit `tab=`** falls back to the tool's own
name, producing a tab literally called "Firewall Administration". The nft-save
action is tab-less, so it lands there alone.

**Fix:** give that action (and any other tab-less firewall action) a real tab —
`tab="nftables"` fits the nft-save one. Then the redundant tab disappears.
Audit all `tool="Firewall Administration"` actions for a missing `tab=` so none
fall through. Decide whether "Nft: save" belongs under nftables or should be
folded into a broader "save/persist" control. (EE + CE — shared actions.py.)

---

## 2. Left-rail icon palette still renders monochrome on the running instance

**Symptom:** The nav rail icons are all one gray color — "no colors in this
palette" — despite per-icon colors having been added in code.

**Root cause (likely):** the color change (per-icon `--rail-icon` hues in
`App.jsx` + `styles.css`) is committed to the branch/`dev` but the running
instance hasn't rebuilt/redeployed the frontend, so it's serving the old
bundle. Not a code defect — a deploy-lag.

**Fix / verify:** rebuild + redeploy the webgui on the instance, then confirm
the rail shows distinct hues. If it's *still* monochrome after a clean deploy,
debug: (a) the inline `style={{ "--rail-icon": ... }}` is present on
`.rail-item`, (b) `.rail-item svg { color: var(--rail-icon, currentColor) }`
isn't being overridden, (c) the active skin isn't one of the intentionally
monochrome ones (amber/phos). (EE + CE.)
