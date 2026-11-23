// meiva:link services/desktop/browser/docs/tab-groups.md
// meiva:link services/desktop/browser/docs/browser-session-restore.md
function meivaShortTitle(title) {
  const match = /^(?:M|Meiva) #(0|[1-9][0-9]*)$/.exec(title);
  return match ? `M #${match[1]}` : null;
}

async function meivaTabOf(targetId) {
  const target = (await chrome.debugger.getTargets()).find((t) => t.id === targetId);
  if (!target || target.tabId === undefined) {
    throw new Error(`no tab for target ${targetId}`);
  }
  return chrome.tabs.get(target.tabId);
}

async function meivaGroupIntoKeeper(groupId, windowId, tabIds) {
  await chrome.tabs.group({ groupId, tabIds });
  const current = await chrome.tabGroups.get(groupId).catch(() => null);
  if (current && current.windowId !== windowId) {
    await chrome.tabGroups.move(groupId, { windowId, index: -1 }).catch(() => {});
  }
}

async function meivaConvergeTitle(title) {
  const groups = (await chrome.tabGroups.query({}))
    .filter((group) => meivaShortTitle(group.title) === title)
    .sort((a, b) => a.id - b.id);
  if (groups.length === 0) {
    return { title, windowId: null, kept: null, groups: [], merged: 0, moved: 0 };
  }
  const sized = [];
  for (const group of groups) {
    const members = await chrome.tabs.query({ groupId: group.id });
    sized.push({ ...group, size: members.length });
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
      await meivaGroupIntoKeeper(keeper.id, keeper.windowId, tabIds);
      moved += tabIds.length;
    }
  }
  if (keeper.title !== title) {
    await chrome.tabGroups.update(keeper.id, { title });
  }
  const settled = await chrome.tabGroups.get(keeper.id).catch(() => null);
  return {
    title,
    windowId: settled ? settled.windowId : keeper.windowId,
    kept: keeper.id,
    groups: sized.map((g) => g.id),
    merged: sized.length - 1,
    moved,
  };
}

self.meivaGroup = async (targetId, title) => {
  const tab = await meivaTabOf(targetId);
  if (tab.groupId !== chrome.tabGroups.TAB_GROUP_ID_NONE) {
    const owner = await chrome.tabGroups.get(tab.groupId).catch(() => null);
    if (!owner || !meivaShortTitle(owner.title)) {
      return {
        title,
        windowId: tab.windowId,
        kept: tab.groupId,
        groups: [tab.groupId],
        merged: 0,
        moved: 0,
        skipped: true,
      };
    }
  }
  let report = await meivaConvergeTitle(title);
  if (report.kept === null) {
    const groupId = await chrome.tabs.group({ tabIds: [tab.id] });
    await chrome.tabGroups.update(groupId, { title });
    report = await meivaConvergeTitle(title);
    if (report.kept === null) {
      return {
        title,
        windowId: tab.windowId,
        kept: groupId,
        groups: [groupId],
        merged: 0,
        moved: 0,
        created: true,
      };
    }
    return { ...report, created: true };
  }
  if (tab.groupId !== report.kept) {
    await meivaGroupIntoKeeper(report.kept, report.windowId, [tab.id]);
    const settled = await chrome.tabGroups.get(report.kept).catch(() => null);
    if (settled) {
      report = { ...report, windowId: settled.windowId };
    }
  }
  return { ...report, created: false };
};

self.meivaConvergeAll = async () => {
  const groups = await chrome.tabGroups.query({});
  const titles = new Set();
  for (const group of groups) {
    const title = meivaShortTitle(group.title);
    if (title) {
      titles.add(title);
    }
  }
  const reports = [];
  for (const title of titles) {
    const report = await meivaConvergeTitle(title);
    if (report.kept !== null) {
      reports.push(report);
    }
  }
  return reports;
};

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
  if (!group || meivaShortTitle(group.title) !== title) {
    return false;
  }
  const members = await chrome.tabs.query({ groupId: group.id });
  if (members.length !== 1) {
    return false;
  }
  await chrome.tabs.ungroup([target.tabId]);
  return true;
};

self.meivaGroupedTargets = async () => {
  const targets = await chrome.debugger.getTargets();
  const tabs = await chrome.tabs.query({});
  const groupOf = new Map(tabs.map((t) => [t.id, t.groupId]));
  const groups = await chrome.tabGroups.query({});
  const titles = new Map(groups.map((g) => [g.id, g.title]));
  const grouped = [];
  for (const target of targets) {
    if (target.type !== "page" || target.tabId === undefined) {
      continue;
    }
    const groupId = groupOf.get(target.tabId);
    if (groupId === undefined || groupId === chrome.tabGroups.TAB_GROUP_ID_NONE) {
      continue;
    }
    grouped.push({ targetId: target.id, title: titles.get(groupId) ?? null });
  }
  return grouped;
};

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
    pinned += 1;
  }
  let grouped = 0;
  for (const group of groups) {
    const matched = [];
    for (const url of group.urls || []) {
      const tab = take(url);
      if (!tab || tab.pinned === true) {
        continue;
      }
      matched.push(tab);
    }
    if (matched.length === 0) {
      continue;
    }
    const title = meivaShortTitle(group.title);
    if (title) {
      const report = await meivaConvergeTitle(title);
      const tabIds = matched.map((t) => t.id);
      if (report.kept === null) {
        const groupId = await chrome.tabs.group({ tabIds });
        await chrome.tabGroups.update(groupId, { title });
        await meivaConvergeTitle(title);
      } else if (matched.some((t) => t.groupId !== report.kept)) {
        await meivaGroupIntoKeeper(report.kept, report.windowId, tabIds);
      }
      grouped += matched.length;
      continue;
    }
    const byWindow = new Map();
    for (const tab of matched) {
      if (!byWindow.has(tab.windowId)) {
        byWindow.set(tab.windowId, []);
      }
      byWindow.get(tab.windowId).push(tab);
    }
    for (const [windowId, members] of byWindow) {
      const existing = (await chrome.tabGroups.query({ title: group.title, windowId })).sort(
        (a, b) => a.id - b.id,
      );
      const tabIds = members.map((t) => t.id);
      if (existing.length === 0) {
        const groupId = await chrome.tabs.group({ tabIds });
        await chrome.tabGroups.update(groupId, { title: group.title });
      } else if (members.some((t) => t.groupId !== existing[0].id)) {
        await chrome.tabs.group({ groupId: existing[0].id, tabIds });
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

const meivaSelfConverge = () => {
  self.meivaConvergeAll().catch(() => {});
};
chrome.runtime.onStartup.addListener(meivaSelfConverge);
chrome.runtime.onInstalled.addListener(meivaSelfConverge);
