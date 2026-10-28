// meiva:note
// Meiva tab-group worker: keeps every page an agent's browser calls create in ONE
// tab group per agent per window, titled `Meiva #<id>`.
//
// The invariant, in full:
//   * A window holds at most one group per agent (Chrome groups cannot span
//     windows, so "one group per agent" is only true per window; the daemon never
//     moves a tab to another window to satisfy a title).
//   * Every grouping starts by CONVERGING that agent's groups in the window: the
//     keeper is the group with the most tabs (ties: the lowest group id), which is
//     a function of the state alone. Running a convergence twice changes nothing
//     the second time, so it is safe to run on every page, on every daemon start
//     and on every extension start/update.
//   * Convergence never touches a group whose title is not `Meiva #<digits>`, and
//     never pulls a tab out of such a group: a group the user made is theirs.
//   * It keeps no state: the title IS the identity, so a daemon reconnect, a
//     Chrome restart or an extension update needs no mapping repair.
//
// Chrome saves, rather than deletes, a group whose last tab closes: the saved
// group moves to the bookmarks bar, `chrome.tabGroups.query` cannot see it, and
// this API has no view or delete for it (measured on Chrome 155:
// `query({saved: true})` is rejected, and the namespace exposes only
// get/move/query/update plus the on* events). `meivaUngroupLast` is what the
// daemon calls before it closes an agent's last page: an ungrouped last tab
// deletes the group instead of saving it. A tab the USER closes by hand never
// passes through that call and still leaves a saved group, which nothing in this
// extension can remove.
//
// This file's NAME carries a hash of its content (see tab_groups/grouping.py):
// Chrome caches an extension's service-worker script in the profile and keeps
// running the cached copy when only the content changes.
// meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
// meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
const MEIVA_TITLE = /^Meiva #\d+$/;

// meiva:note
// The tab behind a CDP target id. `chrome.debugger.getTargets` reads the target
// list without attaching, so no debugger bar ever appears.
async function meivaTabOf(targetId) {
  const target = (await chrome.debugger.getTargets()).find((t) => t.id === targetId);
  if (!target || target.tabId === undefined) {
    throw new Error(`no tab for target ${targetId}`);
  }
  return chrome.tabs.get(target.tabId);
}

// meiva:note
// Converge the groups titled `title` inside ONE window: keep one, move the rest
// into it. Chrome removes a group when its last tab leaves, so the moved groups
// disappear with the move; this function never deletes a group itself.
// It reports what it found, so the daemon can log a duplicate as the defect it is
// instead of repairing it silently.
async function meivaConvergeWindow(title, windowId) {
  const groups = (await chrome.tabGroups.query({ title, windowId })).sort(
    (a, b) => a.id - b.id,
  );
  if (groups.length === 0) {
    return { title, windowId, kept: null, groups: [], merged: 0, moved: 0 };
  }
  const sized = [];
  for (const group of groups) {
    const members = await chrome.tabs.query({ groupId: group.id });
    sized.push({ id: group.id, size: members.length });
  }
  const keeper = [...sized].sort((a, b) => b.size - a.size || a.id - b.id)[0];
  let moved = 0;
  for (const group of sized) {
    if (group.id === keeper.id) {
      continue;
    }
    const members = await chrome.tabs.query({ groupId: group.id });
    const tabIds = members.map((t) => t.id).filter((id) => id !== undefined);
    if (tabIds.length > 0) {
      await chrome.tabs.group({ groupId: keeper.id, tabIds });
      moved += tabIds.length;
    }
  }
  return {
    title,
    windowId,
    kept: keeper.id,
    groups: sized.map((g) => g.id),
    merged: sized.length - 1,
    moved,
  };
}

// meiva:note
// File one page into its agent's group, creating the group on first use.
// Returns the convergence report of the window it touched.
self.meivaGroup = async (targetId, title) => {
  const tab = await meivaTabOf(targetId);
  // meiva:note
  // A tab already in somebody else's group stays there: grouping is presentation,
  // and it must never outrank a decision the user made by hand.
  if (tab.groupId !== chrome.tabGroups.TAB_GROUP_ID_NONE) {
    const owner = await chrome.tabGroups.get(tab.groupId).catch(() => null);
    if (!owner || !MEIVA_TITLE.test(owner.title)) {
      return { title, windowId: tab.windowId, kept: tab.groupId, groups: [tab.groupId], merged: 0, moved: 0, skipped: true };
    }
  }
  let report = await meivaConvergeWindow(title, tab.windowId);
  if (report.kept === null) {
    const groupId = await chrome.tabs.group({ tabIds: [tab.id] });
    await chrome.tabGroups.update(groupId, { title });
    // meiva:note
    // The group has no title between the two calls above, so a grouping that ran
    // in that window in between would not have found it and would have created a
    // second group. Looking again closes that window: the second convergence has
    // nothing left to merge, so the cost is two queries.
    report = await meivaConvergeWindow(title, tab.windowId);
    return { ...report, created: true };
  }
  if (tab.groupId !== report.kept) {
    await chrome.tabs.group({ groupId: report.kept, tabIds: [tab.id] });
  }
  return { ...report, created: false };
};

// meiva:note
// Converge every agent's groups in every window; the daemon calls this at start
// and after an upstream reconnect, and the worker calls it on start/install.
// Returns one report per (title, window) pair that held a group.
self.meivaConvergeAll = async () => {
  const groups = await chrome.tabGroups.query({});
  const windowsByTitle = new Map();
  for (const group of groups) {
    if (!MEIVA_TITLE.test(group.title)) {
      continue;
    }
    if (!windowsByTitle.has(group.title)) {
      windowsByTitle.set(group.title, new Set());
    }
    windowsByTitle.get(group.title).add(group.windowId);
  }
  const reports = [];
  for (const [title, windowIds] of windowsByTitle) {
    for (const windowId of windowIds) {
      reports.push(await meivaConvergeWindow(title, windowId));
    }
  }
  return reports;
};

// meiva:note
// Ungroups the last tab of one of this agent's groups, so that closing it removes
// the group instead of saving it. Returns false, and touches nothing, unless the
// tab is in a group whose title is exactly this agent's AND that group has no
// other member: a group the user made must never be ungrouped.
self.meivaUngroupLast = async (targetId, title) => {
  const target = (await chrome.debugger.getTargets()).find((t) => t.id === targetId);
  if (!target || target.tabId === undefined) {
    return false;
  }
  let group;
  try {
    const tab = await chrome.tabs.get(target.tabId);
    if (tab.groupId === chrome.tabGroups.TAB_GROUP_ID_NONE) {
      return false;
    }
    group = await chrome.tabGroups.get(tab.groupId);
  } catch (error) {
    return false;
  }
  if (!group || group.title !== title) {
    return false;
  }
  const members = await chrome.tabs.query({ groupId: group.id });
  if (members.length !== 1) {
    return false;
  }
  await chrome.tabs.ungroup([target.tabId]);
  return true;
};

// meiva:note
// Every open window's tabs in strip order, with the two layout attributes that a
// browser restart does not always bring back: `pinned` and the tab group's title.
// Read-only, and the only thing that looks at windows: the stop-time session
// ledger is a record, never a plan to re-create tabs.
// meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
self.meivaLayout = async () => {
  const windows = await chrome.windows.getAll({ populate: true });
  const groups = await chrome.tabGroups.query({});
  const titles = new Map(groups.map((g) => [g.id, g.title]));
  return windows.map((w) =>
    (w.tabs || []).map((t) => ({
      index: t.index,
      url: t.url || "",
      title: t.title || "",
      pinned: t.pinned === true,
      group:
        t.groupId === chrome.tabGroups.TAB_GROUP_ID_NONE ? null : titles.get(t.groupId) || null,
      active: t.active === true,
    })),
  );
};

// meiva:note
// Re-applies the two attributes above from the stop-time ledger: pin the tabs
// recorded as pinned, then put each recorded group back by title, one group per
// window (the same rule `meivaGroup` follows). Idempotent by construction -- a tab
// already pinned stays pinned, and a group whose members already match is left
// alone -- so a Chrome that restored the layout itself costs nothing. It never
// closes, moves, unpins or ungroups a tab: restoring what was there must not
// destroy what is. Tabs are matched by URL in strip order, so two tabs with the
// same URL keep their own pin and group.
// meiva:link services/desktop/browser/docs/browser-session-restore.meiva.okf.md
self.meivaApplyLayout = async (spec) => {
  const pins = spec.pins || [];
  const groups = spec.groups || [];
  const windows = await chrome.windows.getAll({ populate: true });
  const tabs = (windows || []).flatMap((w) => w.tabs || []);
  const used = new Set();
  const take = (url) => {
    const tab = tabs.find((t) => !used.has(t.id) && (t.url || "") === url);
    if (tab) {
      used.add(tab.id);
    }
    return tab || null;
  };
  let pinned = 0;
  for (const url of pins) {
    const tab = take(url);
    if (!tab) {
      continue;
    }
    if (tab.pinned !== true) {
      await chrome.tabs.update(tab.id, { pinned: true });
    }
    // meiva:note
    // Counted when the tab IS pinned, not only when this call pinned it: a
    // Chrome that restored the pin itself leaves nothing to do, and the caller
    // reports "how many recorded attributes are in place".
    pinned += 1;
  }
  let grouped = 0;
  for (const group of groups) {
    const byWindow = new Map();
    for (const url of group.urls || []) {
      const tab = take(url);
      if (!tab || tab.pinned === true) {
        continue;
      }
      if (!byWindow.has(tab.windowId)) {
        byWindow.set(tab.windowId, []);
      }
      byWindow.get(tab.windowId).push(tab);
    }
    for (const [windowId, members] of byWindow) {
      const report = await meivaConvergeWindow(group.title, windowId);
      const tabIds = members.map((t) => t.id);
      if (report.kept === null) {
        const groupId = await chrome.tabs.group({ tabIds });
        await chrome.tabGroups.update(groupId, { title: group.title });
      } else if (members.some((t) => t.groupId !== report.kept)) {
        await chrome.tabs.group({ groupId: report.kept, tabIds });
      }
      grouped += members.length;
    }
  }
  return {
    pinned,
    grouped,
    expectedPinned: pins.length,
    expectedGrouped: groups.reduce((n, g) => n + (g.urls || []).length, 0),
  };
};

// meiva:note
// A Chrome restart and an extension update both start this worker from scratch.
// Converging here means neither can leave an agent with two groups, without the
// daemon having to remember anything.
// meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
const meivaSelfConverge = () => {
  self.meivaConvergeAll().catch(() => {});
};
chrome.runtime.onStartup.addListener(meivaSelfConverge);
chrome.runtime.onInstalled.addListener(meivaSelfConverge);
