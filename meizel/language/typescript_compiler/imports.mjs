// meiva:note
// Import syntax comes from the compiler; policy resolution never promotes untracked files to facts.
// meiva:link meizel/docs/checker/typescript.meiva.okf.md
export function imports(ts, context, tree, result) {
  const bindings = new Map();
  const line = node => tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1;
  const record = (literal, node, names, kind = 'import') => {
    if (!literal || !ts.isStringLiteralLike(literal)) return undefined;
    const spec = literal.text;
    const module = context.resolve(spec, tree.fileName);
    const dst = module && context.relative(module.resolvedFileName);
    if (dst && context.tracked.has(dst)) {
      if (dst !== result.path) result.facts.push([dst, line(node), kind, names, `${kind} from ${JSON.stringify(spec)}`]);
      return {target: dst, firstParty: true, spec};
    }
    if (context.localSpecifier(spec, tree.fileName)) {
      result.problems.push([line(node), 'unresolved-import', `cannot resolve first-party TypeScript import ${JSON.stringify(spec)}`]);
      return {target: spec, firstParty: false, unresolved: true, spec};
    }
    result.external.push(spec.startsWith('@') ? spec.split('/').slice(0, 2).join('/') : spec.split('/')[0]);
    return {target: spec, firstParty: false, spec};
  };
  function bind(identifier, origin, member) {
    const symbol = context.checker.getSymbolAtLocation(identifier);
    if (symbol) bindings.set(symbol, {...origin, member});
  }
  function requireCall(node) {
    if (!ts.isIdentifier(node.expression) || node.expression.text !== 'require') return false;
    const symbol = context.checker.getSymbolAtLocation(node.expression);
    return !symbol?.declarations?.some(declaration => !declaration.getSourceFile().isDeclarationFile);
  }
  function visit(node) {
    if (ts.isImportDeclaration(node)) {
      const clause = node.importClause;
      const named = clause?.namedBindings;
      const names = named && ts.isNamedImports(named) ? named.elements.map(element => (element.propertyName ?? element.name).text) : [];
      const origin = record(node.moduleSpecifier, node, names);
      if (origin && clause) {
        if (clause.name) bind(clause.name, origin, 'default');
        if (named && ts.isNamespaceImport(named)) bind(named.name, origin, '');
        if (named && ts.isNamedImports(named)) for (const element of named.elements) bind(element.name, origin, (element.propertyName ?? element.name).text);
      }
    } else if (ts.isExportDeclaration(node) && node.moduleSpecifier) {
      const names = node.exportClause && ts.isNamedExports(node.exportClause) ? node.exportClause.elements.map(element => (element.propertyName ?? element.name).text) : [];
      record(node.moduleSpecifier, node, names);
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      const origin = record(node.moduleReference.expression, node, []);
      if (origin) bind(node.name, origin, '');
    } else if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)) {
      record(node.argument.literal, node, []);
    } else if (ts.isCallExpression(node) && (node.expression.kind === ts.SyntaxKind.ImportKeyword || requireCall(node))) {
      if (node.arguments[0] && ts.isStringLiteralLike(node.arguments[0])) {
        const parent = ts.isAwaitExpression(node.parent) ? node.parent.parent : node.parent;
        const declaration = (requireCall(node) || ts.isAwaitExpression(node.parent)) && ts.isVariableDeclaration(parent) ? parent : undefined;
        const named = declaration && ts.isObjectBindingPattern(declaration.name) ? declaration.name.elements : [];
        const names = named.filter(element => !element.dotDotDotToken).map(element => (element.propertyName ?? element.name).getText(tree));
        const origin = record(node.arguments[0], node, names);
        if (origin && declaration) {
          if (ts.isIdentifier(declaration.name)) bind(declaration.name, origin, '');
          for (const element of named) if (ts.isIdentifier(element.name)) bind(element.name, origin, element.dotDotDotToken ? '' : (element.propertyName ?? element.name).getText(tree));
        }
      }
      else result.restricted.push([line(node), requireCall(node) ? 'require()' : 'import()', 'computed target']);
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  return {bindings, record};
}
