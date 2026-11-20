// meiva:note
// The workspace file open in the right-hand context panel. The address is its source of truth:
// opening a file pushes a history entry, the panel's Back is the browser's Back, and a traversal
// (Back / Forward) re-reads the file from the address (user ruling 2026-11-20). The navigator is the
// binding to the browser's history that `useWorkspaceFileHistory` installs; without one (a surface
// with no page history) the store only holds the open file.
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
  navigator: WorkspaceFileNavigator | null;
  openFile: (target: WorkspaceFileTarget) => void;
  // meiva:note
  // Leave the file for the to-user document as a new entry (Back returns to the file).
  home: () => void;
  goBack: () => void;
  // meiva:note
  // The panel closed: the file leaves the current entry without a new one.
  closeFile: () => void;
}

const sameFile = (a: WorkspaceFileTarget | null, b: WorkspaceFileTarget) =>
  a !== null && a.agentId === b.agentId && a.path === b.path;

export const useWorkspaceFileStore = create<WorkspaceFileStore>((set, get) => ({
  activeFile: null,
  navigator: null,

  openFile: (target) => {
    const { activeFile, navigator } = get();
    set({ activeFile: target });
    navigator?.show(target, sameFile(activeFile, target) ? "replace" : "push");
  },

  home: () => {
    set({ activeFile: null });
    get().navigator?.show(null, "push");
  },

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
}));
