import { beforeEach, describe, expect, it } from "vitest";
import { useWorkspaceFileStore } from "./workspace-file-store";

describe("useWorkspaceFileStore", () => {
  beforeEach(() => {
    useWorkspaceFileStore.getState().closeFile();
  });

  it("opens a file and updates activeFile", () => {
    const store = useWorkspaceFileStore.getState();
    store.openFile({ agentId: 1, path: "src/main.py", lineRange: "10-20" });

    const current = useWorkspaceFileStore.getState().activeFile;
    expect(current).toEqual({ agentId: 1, path: "src/main.py", lineRange: "10-20" });
    expect(useWorkspaceFileStore.getState().history).toEqual([]);
  });

  it("pushes to history when opening a subsequent file and navigates back", () => {
    const store = useWorkspaceFileStore.getState();
    store.openFile({ agentId: 1, path: "src/main.py" });
    store.openFile({ agentId: 1, path: "docs/readme.md" });

    expect(useWorkspaceFileStore.getState().activeFile?.path).toBe("docs/readme.md");
    expect(useWorkspaceFileStore.getState().history).toHaveLength(1);
    expect(useWorkspaceFileStore.getState().history[0].path).toBe("src/main.py");

    store.goBack();
    expect(useWorkspaceFileStore.getState().activeFile?.path).toBe("src/main.py");
    expect(useWorkspaceFileStore.getState().history).toHaveLength(0);

    store.goBack();
    expect(useWorkspaceFileStore.getState().activeFile).toBeNull();
  });

  it("closeFile clears both activeFile and history", () => {
    const store = useWorkspaceFileStore.getState();
    store.openFile({ agentId: 1, path: "a.py" });
    store.openFile({ agentId: 1, path: "b.py" });
    store.closeFile();

    expect(useWorkspaceFileStore.getState().activeFile).toBeNull();
    expect(useWorkspaceFileStore.getState().history).toEqual([]);
  });
});
