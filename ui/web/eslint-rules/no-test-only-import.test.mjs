// meiva:note
// Behavior lock for the local no-test-only-import rule: a production module may not reach a test double, a test-support module or a test file. The valid cases pin the limits of the naming convention, since a rule that fires on a helper whose name merely contains "test" would be turned off rather than obeyed.
// meiva:link ui/web/src/docs/frontend-components/test-only-imports.meiva.okf.md
import { RuleTester } from "eslint";
import { describe, it } from "vitest";

import rule from "./no-test-only-import.mjs";

RuleTester.describe = describe;
RuleTester.it = it;

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

tester.run("no-test-only-import", rule, {
  valid: [
    { code: 'import { Card } from "./card";' },
    { code: 'import { api } from "@/lib/transport/api";' },
    { code: 'import { runner } from "./test-mock-utils";' },
    { code: 'import { helpers } from "./testing/fixtures";' },
    { code: 'const module = await import("./lazy-chunk");' },
  ],
  invalid: [
    {
      code: 'import { pageRig } from "./page-test-mock";',
      errors: [{ messageId: "testOnly" }],
    },
    {
      code: 'import { rig } from "@/components/inspector/inspector-test-mock";',
      errors: [{ messageId: "testOnly" }],
    },
    {
      code: 'import { fixtures } from "./test-support/fixtures";',
      errors: [{ messageId: "testOnly" }],
    },
    {
      code: 'export { rig } from "./sidebar-test-mock.tsx";',
      errors: [{ messageId: "testOnly" }],
    },
    {
      code: 'export * from "./test-support";',
      errors: [{ messageId: "testOnly" }],
    },
    {
      code: 'const stub = await import("./chart.test.ts");',
      errors: [{ messageId: "testOnly" }],
    },
  ],
});
