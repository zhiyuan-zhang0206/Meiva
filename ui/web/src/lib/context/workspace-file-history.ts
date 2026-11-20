"use client";

import { useEffect, useRef } from "react";

import { useBrowser } from "@/lib/browser/browser-provider";

import { PUSHED_ENTRY, readView, withView } from "./reference-url";
import { useWorkspaceFileStore } from "./workspace-file-store";

function asRecord(state: unknown): object {
  return typeof state === "object" && state !== null ? state : {};
}

// meiva:note
// Binds the workspace file store to the page's history: the file in the address opens on load (a
// shared or reloaded link shows the same file), opening one pushes an entry, Back and Forward
// re-read it, and choosing another agent closes it (the agent switch strips it from the address).
// Mounted once, by the home view.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.md
export function useWorkspaceFileHistory(activeId: number | null): void {
  const navigation = useBrowser().navigation;
  useEffect(() => {
    const read = () => {
      const where = navigation.location();
      return where === null ? null : readView(new URL(where.href));
    };
    useWorkspaceFileStore.getState().restore(read());
    useWorkspaceFileStore.setState({
      navigator: {
        show(target, mode) {
          const where = navigation.location();
          if (where === null) return;
          const url = withView(where.href, target);
          if (mode === "push") navigation.push(url, { ...asRecord(navigation.state()), [PUSHED_ENTRY]: true });
          else navigation.replace(url, navigation.state());
        },
        back() {
          if (Reflect.get(asRecord(navigation.state()), PUSHED_ENTRY) !== true) return false;
          navigation.back();
          return true;
        },
      },
    });
    const stop = navigation.onTraverse(() => useWorkspaceFileStore.getState().restore(read()));
    return () => {
      stop();
      useWorkspaceFileStore.setState({ navigator: null });
    };
  }, [navigation]);

  // meiva:note
  // Only a switch between two agents closes the file: the first selection on load (null to the
  // address's agent) keeps the file a shared link opened. The last agent seen is remembered across a
  // passing null, so a switch through "no agent" still closes it.
  const previous = useRef(activeId);
  useEffect(() => {
    if (activeId === null) return;
    const before = previous.current;
    previous.current = activeId;
    if (before === null || before === activeId) return;
    useWorkspaceFileStore.setState({ activeFile: null });
  }, [activeId]);
}
