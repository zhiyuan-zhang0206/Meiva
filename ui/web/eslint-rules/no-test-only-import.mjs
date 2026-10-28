// meiva:note
// A shipped module must not import a test-only one: a *-test-mock module, anything under test-support/, or a *.test / *.spec file. The import would drag the double's fixtures and fake transports into the bundle and turn it into a production dependency nothing would notice going stale. Tests and the doubles themselves are the intended importers, so the config scopes this rule to non-test files.
// meiva:link ui/web/src/docs/frontend-components/test-only-imports.meiva.okf.md
const TEST_ONLY = [
  /(?:^|\/)[^/]*-test-mock(?:\.[cm]?[jt]sx?)?$/,
  /(?:^|\/)test-support(?:\/|$)/,
  /\.test(?:\.[cm]?[jt]sx?)?$/,
  /\.spec(?:\.[cm]?[jt]sx?)?$/,
];

export default {
  meta: {
    type: "problem",
    docs: {
      description:
        "Forbid a production module from importing a test-only module (test-mock, test-support, *.test/*.spec).",
    },
    schema: [],
    messages: {
      testOnly: "'{{source}}' is a test-only module; production code must not import it.",
    },
  },
  create(context) {
    function check(node) {
      const source = node.source?.value;
      if (typeof source !== "string") return;
      if (!TEST_ONLY.some((pattern) => pattern.test(source))) return;
      context.report({ node, messageId: "testOnly", data: { source } });
    }

    return {
      ImportDeclaration: check,
      ExportNamedDeclaration: check,
      ExportAllDeclaration: check,
      ImportExpression: check,
    };
  },
};
