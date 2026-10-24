import { describe, expect, it } from "vitest";

import { agentShellPath, commandsPath, inspectStatisticsPath, listAgentsPath, tasksPath, toUserPath } from "./agent-routes";

describe("agent routes", () => {
  it("listAgents defaults to the SQL-bounded live roster", () => {
    expect(listAgentsPath({})).toBe("/api/agents?scope=live&limit=100");
  });

  it("listAgents requests terminated history only when explicit", () => {
    expect(listAgentsPath({ scope: "terminated" })).toBe("/api/agents?scope=terminated&limit=100");
  });

  it("listAgents carries the search, cursor and page size", () => {
    expect(listAgentsPath({ scope: "all", query: "fix bug", beforeId: 90, limit: 25 }))
      .toBe("/api/agents?scope=all&limit=25&query=fix+bug&before_id=90");
  });

  it("getCommands is global without query params and scoped to an agent on request", () => {
    expect(commandsPath()).toBe("/api/commands");
    expect(commandsPath(null)).toBe("/api/commands");
    expect(commandsPath(7)).toBe("/api/commands?agent_id=7");
  });

  it("inspector statistics take the hours window only when given", () => {
    expect(inspectStatisticsPath(7, 24)).toBe("/api/agents/7/inspect/statistics?hours=24");
    expect(inspectStatisticsPath(7)).toBe("/api/agents/7/inspect/statistics");
    expect(inspectStatisticsPath(7, null)).toBe("/api/agents/7/inspect/statistics");
  });

  it("a shell capture takes the line count only when given", () => {
    expect(agentShellPath(3, 9)).toBe("/api/agents/3/shell/9");
    expect(agentShellPath(3, 9, 50)).toBe("/api/agents/3/shell/9?lines=50");
  });

  it("the to-user document repeats one reference parameter per reference", () => {
    expect(toUserPath(5, [])).toBe("/api/agents/5/to-user");
    expect(toUserPath(5, ["a", "b c"])).toBe("/api/agents/5/to-user?reference=a&reference=b+c");
  });

  it("tasks take a window and a field set", () => {
    expect(tasksPath()).toBe("/api/tasks");
    expect(tasksPath({ window: "7d", fields: "summary" })).toBe("/api/tasks?window=7d&fields=summary");
  });
});
