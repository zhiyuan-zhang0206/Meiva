// meiva:note
// Meiva tab-group worker: files one CDP target into the tab group titled
// `title`, creating the group on first use and coalescing any duplicate open
// groups with the same title into the first one. Stateless on purpose -- the
// group is found by its title, so no groupId mapping has to survive a daemon
// reconnect or a Chrome restart. The caller guarantees the title is unique per
// agent. Colors are left to Chrome.
// Chrome does not delete a group when its last tab closes: it keeps the group
// as a SAVED (closed) group on the bookmarks bar. `chrome.tabGroups.query`
// returns only open groups, and this API has no saved-group view and no delete
// (measured on Chrome 155: query({saved:true}) is rejected, and the namespace
// exposes only get/move/query/update plus the on* events). A saved group is
// therefore invisible to the title lookup below, so the next page for that
// agent would create a second group and every open/close cycle would leave one
// more closed group behind -- in the user's own Chrome, because saved groups
// sync. `meivaUngroupLast` is what the daemon calls before it closes an agent's
// last page: an ungrouped last tab deletes the group instead of saving it.
// A tab the USER closes by hand never passes through that call and still leaves
// a saved group, which nothing in this extension can remove.
// This file's NAME carries a hash of its content (see tab_groups/grouping.py):
// Chrome caches an extension's service-worker script in the profile and keeps
// running the cached copy when only the content changes.
// meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
self.meivaGroup = async (targetId, title) => {
  const target = (await chrome.debugger.getTargets()).find((t) => t.id === targetId);
  if (!target || target.tabId === undefined) {
    throw new Error(`no tab for target ${targetId}`);
  }
  const [existing, ...duplicates] = await chrome.tabGroups.query({ title });
  if (existing) {
    const extraTabs = (
      await Promise.all(duplicates.map((g) => chrome.tabs.query({ groupId: g.id })))
    ).flat();
    const tabIds = [
      target.tabId,
      ...extraTabs.map((t) => t.id).filter((id) => id !== undefined && id !== target.tabId),
    ];
    return chrome.tabs.group({ groupId: existing.id, tabIds });
  }
  const groupId = await chrome.tabs.group({ tabIds: [target.tabId] });
  await chrome.tabGroups.update(groupId, { title });
  return groupId;
};

// meiva:note
// Ungroups the last tab of one of this agent's groups, so that closing it removes
// the group instead of saving it. Returns false, and touches nothing, unless the
// tab is in a group whose title is exactly this agent's AND that group has no
// other member: a group the user made must never be ungrouped.
// meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
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
