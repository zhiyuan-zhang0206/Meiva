// meiva:note
// State store for active workspace file viewing in the right-hand context panel.
// Opening a file target updates the active selection and appends to navigation history.
"use client";

import { create } from "zustand";

export interface WorkspaceFileTarget {
  agentId: number;
  path: string;
  lineRange?: string;
}

export interface WorkspaceFileStore {
  activeFile: WorkspaceFileTarget | null;
  history: WorkspaceFileTarget[];
  openFile: (target: WorkspaceFileTarget) => void;
  closeFile: () => void;
  goBack: () => void;
}

export const useWorkspaceFileStore = create<WorkspaceFileStore>((set, get) => ({
  activeFile: null,
  history: [],

  openFile: (target: WorkspaceFileTarget) => {
    const current = get().activeFile;
    const history = get().history;
    const nextHistory =
      current && (current.agentId !== target.agentId || current.path !== target.path)
        ? [...history, current]
        : history;

    set({
      activeFile: target,
      history: nextHistory,
    });
  },

  closeFile: () => {
    set({
      activeFile: null,
      history: [],
    });
  },

  goBack: () => {
    const history = get().history;
    if (history.length === 0) {
      set({ activeFile: null, history: [] });
      return;
    }
    const previous = history[history.length - 1];
    set({
      activeFile: previous,
      history: history.slice(0, -1),
    });
  },
}));
