// meiva:note
// The files open in the right-hand panel: one tab list per conversation, the file on show (null is
// the conversation's to-user document), the recently opened files, and whether the panel fills the
// page. The address is the source of truth for the file on show: opening or selecting a file pushes a
// history entry, the panel's Back is the browser's Back, and a traversal (Back / Forward) re-reads
// it (user ruling 2026-11-20). The navigator is the binding to the browser's history that
// `useWorkspaceFileHistory` installs; without one (a surface with no page history) the store only
// holds the state.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.md
"use client";

import { create } from "zustand";

import type { ViewTarget } from "./reference-url";

export type WorkspaceFileTarget = ViewTarget;

export interface WorkspaceFileNavigator {
  show: (target: WorkspaceFileTarget | null, mode: "push" | "replace") => void;
  // meiva:note
  // Steps back through browser history when the current entry is the panel's; false otherwise.
  back: () => boolean;
}

export interface WorkspaceFileStore {
  activeFile: WorkspaceFileTarget | null;
  tabs: Record<number, WorkspaceFileTarget[]>;
  recent: WorkspaceFileTarget[];
  maximized: boolean;
  navigator: WorkspaceFileNavigator | null;
  // meiva:note
  // Open a file in its conversation's tabs and show it; a new line range of an open file replaces
  // the entry instead of adding one.
  openFile: (target: WorkspaceFileTarget) => void;
  // meiva:note
  // Show an open tab, or the to-user document for null, as a new entry.
  selectTab: (target: WorkspaceFileTarget | null) => void;
  // meiva:note
  // Close a tab; closing the one on show moves to its neighbour (or the document) in place.
  closeTab: (target: WorkspaceFileTarget) => void;
  home: () => void;
  goBack: () => void;
  // meiva:note
  // The panel closed: the file leaves the current entry without a new one; its tabs stay.
  closeFile: () => void;
  // meiva:note
  // The address named this file (load, Back, Forward): show it and keep it among the tabs.
  restore: (target: WorkspaceFileTarget | null) => void;
  setMaximized: (maximized: boolean) => void;
}

const RECENT_MAX = 20;

// meiva:note
// The agent whose machine reads the file: the conversation's own unless the target names another.
export const machineOf = (target: WorkspaceFileTarget) => target.machineAgentId ?? target.agentId;

export const sameFile = (a: WorkspaceFileTarget | null, b: WorkspaceFileTarget) =>
  a !== null && a.agentId === b.agentId && machineOf(a) === machineOf(b) && a.path === b.path;

// meiva:note
// A machine that is the conversation's own is not named, so one file has one form.
function plain(target: WorkspaceFileTarget): WorkspaceFileTarget {
  if (target.machineAgentId !== target.agentId) return target;
  return { agentId: target.agentId, path: target.path, ...(target.lineRange ? { lineRange: target.lineRange } : {}) };
}

function withTab(tabs: Record<number, WorkspaceFileTarget[]>, target: WorkspaceFileTarget) {
  const list = tabs[target.agentId] ?? [];
  const index = list.findIndex((tab) => sameFile(tab, target));
  const next = index < 0 ? [...list, target] : list.map((tab, i) => (i === index ? target : tab));
  return { ...tabs, [target.agentId]: next };
}

function withRecent(recent: WorkspaceFileTarget[], target: WorkspaceFileTarget) {
  return [target, ...recent.filter((file) => !sameFile(file, target))].slice(0, RECENT_MAX);
}

export const useWorkspaceFileStore = create<WorkspaceFileStore>((set, get) => ({
  activeFile: null,
  tabs: {},
  recent: [],
  maximized: false,
  navigator: null,

  openFile: (opened) => {
    const target = plain(opened);
    const { activeFile, tabs, recent, navigator } = get();
    set({ activeFile: target, tabs: withTab(tabs, target), recent: withRecent(recent, target) });
    navigator?.show(target, sameFile(activeFile, target) ? "replace" : "push");
  },

  selectTab: (target) => {
    const { activeFile, navigator } = get();
    if (target === null ? activeFile === null : sameFile(activeFile, target)) return;
    set({ activeFile: target });
    navigator?.show(target, "push");
  },

  closeTab: (target) => {
    const { activeFile, tabs, navigator } = get();
    const list = tabs[target.agentId] ?? [];
    const index = list.findIndex((tab) => sameFile(tab, target));
    if (index < 0) return;
    const rest = list.filter((_, i) => i !== index);
    set({ tabs: { ...tabs, [target.agentId]: rest } });
    if (!sameFile(activeFile, target)) return;
    const next = rest.at(index) ?? (index > 0 ? rest.at(index - 1) : undefined) ?? null;
    set({ activeFile: next });
    navigator?.show(next, "replace");
  },

  home: () => get().selectTab(null),

  goBack: () => {
    if (get().navigator?.back() === true) return;
    set({ activeFile: null });
    get().navigator?.show(null, "replace");
  },

  closeFile: () => {
    if (get().activeFile === null) return;
    set({ activeFile: null });
    get().navigator?.show(null, "replace");
  },

  restore: (target) => {
    set(target === null
      ? { activeFile: null }
      : { activeFile: target, tabs: withTab(get().tabs, target) });
  },

  setMaximized: (maximized) => set({ maximized }),
}));
