import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const ICON_CONVENTION =
  /^(favicon\.ico|icon\d?\.(ico|jpe?g|png|svg|tsx?|jsx?)|apple-icon\d?\.(jpe?g|png|tsx?|jsx?))$/i;

function iconConventionFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return iconConventionFiles(path);
    return ICON_CONVENTION.test(entry.name) ? [path] : [];
  });
}

describe("brand favicon and app icons", () => {
  it("ships the official brand icon.svg under src/app", () => {
    expect(iconConventionFiles("src/app")).toEqual(["src/app/icon.svg"]);
    expect(existsSync("public/favicon.ico")).toBe(false);
    const svg = readFileSync("src/app/icon.svg", "utf8");
    expect(svg).toContain('viewBox="0 0 1000 1000"');
    expect(svg).toContain("#ED8755");
    expect(svg).toContain("#427C6E");
    expect(svg).toContain("#2276B5");
  });

  it("matches every icon convention Next accepts", () => {
    for (const name of ["favicon.ico", "icon.png", "icon1.png", "apple-icon1.tsx"]) {
      expect(ICON_CONVENTION.test(name), name).toBe(true);
    }
    for (const name of ["icon12.png", "favicon1.ico", "icon-abc123.png"]) {
      expect(ICON_CONVENTION.test(name), name).toBe(false);
    }
  });
});
