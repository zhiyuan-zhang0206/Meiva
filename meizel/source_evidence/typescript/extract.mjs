// meiva:note
// This adapter owns only holder-initializer evidence; it does not copy note ownership traversal.
// meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md

export function sourceEvidence(ts, checker, tree, libraryPaths, relative) {
  const line = node => tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1;
  const endLine = node => tree.getLineAndCharacterOfPosition(Math.max(node.getStart(tree), node.end - 1)).line + 1;
  const declarations = node => {
    const symbol = checker.getSymbolAtLocation(node);
    if (!symbol || symbol.flags & ts.SymbolFlags.Alias || !symbol.declarations?.length) return null;
    const records = [];
    for (const declaration of symbol.declarations) {
      const file = declaration.getSourceFile();
      const dependency = libraryPaths.get(file.fileName);
      if (!dependency) return null;
      records.push({dependency, line:file.getLineAndCharacterOfPosition(declaration.getStart(file)).line + 1});
    }
    return records;
  };
  const unknown = (node, binding, properties, reason) => ({
    line:line(node), end_line:endLine(node), binding, properties, kind:'unknown',
    constructor:'unknown', declarations:[], frozen_surface:false, freeze_receiver:[], freeze_callee:[], reason,
  });
  const unwrap = node => {
    while (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isSatisfiesExpression(node) || ts.isTypeAssertionExpression(node)) node = node.expression;
    return node;
  };
  const inspect = (original, binding, properties, freeze) => {
    const node = unwrap(original);
    if (ts.isNewExpression(node)) {
      const expression = unwrap(node.expression);
      const name = ts.isIdentifier(expression) ? expression.text : '';
      const provenance = declarations(expression);
      if (!['Map','Set'].includes(name) || !provenance) {
        const symbol = checker.getSymbolAtLocation(expression);
        const places = (symbol?.declarations ?? []).map(d => {
          const file = d.getSourceFile();
          return `${relative(file.fileName)}:${file.getLineAndCharacterOfPosition(d.getStart(file)).line + 1}`;
        });
        return [unknown(node,binding,properties,`constructor provenance unknown or source-shadowed${places.length ? '; declarations '+places.join(', ') : ''}; spelling is not native identity`)];
      }
      return [{line:line(node),end_line:endLine(node),binding,properties,kind:'module-allocation',constructor:name,
        declarations:provenance, frozen_surface:Boolean(freeze),freeze_receiver:freeze?.receiver ?? [],freeze_callee:freeze?.callee ?? [],
        reason:'SourceFile-direct binding initializer reaches a standard-library-declared collection; runtime replacement, actual sharing and writes remain unknown'}];
    }
    if (ts.isObjectLiteralExpression(node)) {
      const names = new Set();
      for (const property of node.properties) {
        if (!ts.isPropertyAssignment(property) || (!ts.isIdentifier(property.name) && !ts.isStringLiteral(property.name)) || names.has(property.name.text)) {
          return [unknown(node,binding,properties,'whole holder path unknown: spread, duplicate, computed, shorthand or accessor property')];
        }
        names.add(property.name.text);
      }
      return node.properties.flatMap(property => inspect(property.initializer,binding,[...properties,property.name.text],null));
    }
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'freeze') {
      const receiver = declarations(node.expression.expression);
      const callee = declarations(node.expression);
      if (!ts.isIdentifier(node.expression.expression) || node.expression.expression.text !== 'Object' || !receiver || !callee || node.arguments.length !== 1 || freeze) {
        return [unknown(node,binding,properties,'whole holder path unknown: freeze receiver/callee provenance, arguments or nested wrapper unsupported')];
      }
      return inspect(node.arguments[0],binding,properties,{receiver,callee});
    }
    return [unknown(node,binding,properties,`initializer ${ts.SyntaxKind[node.kind]} is outside the supported holder path; returned identity, aliases and effects unknown`)];
  };
  const observations = [];
  for (const statement of tree.statements) {
    if (ts.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (ts.isIdentifier(declaration.name) && declaration.initializer) {
          observations.push(...inspect(declaration.initializer,declaration.name.text,[],null));
        } else observations.push(unknown(declaration,'',[],'binding pattern or missing initializer unsupported'));
      }
    } else if (!ts.isImportDeclaration(statement) && !ts.isExportDeclaration(statement)) {
      observations.push(unknown(statement,'',[],`unsupported execution region ${ts.SyntaxKind[statement.kind]}; function-local allocations are not automatically module shared or pure`));
    }
  }
  return observations;
}
