# Sysible Controller — UX / polish backlog

Small issues to fix later, captured during lab testing. Newest first.
Each item: symptom → root cause (where known) → suggested fix.

---

## 3. Settings: place the Controller IP card to the RIGHT of Software updates

**Symptom / request:** In Settings → Controller the "Controller IP" card
(Address mode / IP address / Port / Regenerate cert+bundle) and the "Software
updates" card are stacked vertically, wasting the wide right-hand space.

**Request:** lay them side by side — Software updates on the left, the
Controller IP card to its right — so both are visible without scrolling.

**Where:** `webgui/frontend/src/views/Settings.jsx` — the Controller IP block
(~L793, "The controller is addressed by IP…") and the Software updates block
(~L1142, `<strong>Software updates</strong>`). Wrap the two cards in a
two-column flex/grid row that collapses back to stacked on narrow widths.
(EE + CE — Settings is shared.)

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

## 2. Left-rail per-icon colors — tried, then REVERTED by request

**History:** per-icon rail colors were added, then removed at the user's
request — "makes it look like a toy." The rail is back to its original look:
monochrome icons that take the skin accent only on the active item.

**Decision / if revisited:** distinct per-icon hues read as toy-like here. Any
future attempt should be subtler — e.g. accent-tint only, or color solely the
active item (already the case) — not a full rainbow. Reverted in both editions
(`App.jsx` RAIL_ICON_COLORS + the `.rail-item svg` hue CSS). Leave monochrome
unless the user explicitly asks again. (EE + CE.)
