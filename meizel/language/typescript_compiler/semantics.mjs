// meiva:note
// Shared rule vocabulary is emitted from compiler nodes and lexical symbols, never source-text matching.
// meiva:link meizel/docs/checker/typescript.meiva.okf.md
export function semantics(ts, context, tree, out, index, config, test, ambientPolicy) {
  const checker = context.checker;
  const impure = new Set(config.impure_calls);
  // meiva:note
  // The page and the browser the code runs in: its address, the storage it keeps, the cookies and the machine it reports,
  // bare or behind `window.`, `globalThis.` or `self.`. Only the global counts: a package called `navigator` does not.
  const browserReads = Object.fromEntries(['', 'window.', 'globalThis.', 'self.'].flatMap(base => Object.entries({
    location: 'environ', localStorage: 'environ', sessionStorage: 'environ', navigator: 'platform', matchMedia: 'platform',
    'document.location': 'environ', 'document.cookie': 'environ',
  }).map(([name, kind]) => [base + name, kind])));
  const hooks = new Set(config.impure_hooks);
  const line = node => tree.getLineAndCharacterOfPosition(node.getStart(tree)).line + 1;
  const column = node => Buffer.byteLength(tree.text.slice(tree.getPositionOfLineAndCharacter(line(node) - 1, 0), node.getStart(tree)), 'utf8');
  const flags = node => ts.getCombinedModifierFlags(node);
  const exported = node => Boolean(flags(node) & ts.ModifierFlags.Export);
  const nameOf = node => node.name && (ts.isIdentifier(node.name) || ts.isStringLiteralLike(node.name) || ts.isPrivateIdentifier(node.name)) ? node.name.text : '';
  const transparent = node => {
    while (node && (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) || ts.isTypeAssertionExpression(node) || ts.isSatisfiesExpression(node) || ts.isNonNullExpression(node))) node = node.expression;
    return node;
  };
  const functionNode = node => ts.isFunctionDeclaration(node) || ts.isFunctionExpression(node) || ts.isArrowFunction(node) || ts.isMethodDeclaration(node) || ts.isConstructorDeclaration(node) || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node) || ts.isFunctionTypeNode(node) || ts.isMethodSignature(node) || ts.isCallSignatureDeclaration(node);
  function ownName(node) {
    if (ts.isConstructorDeclaration(node)) return 'constructor';
    if (ts.isFunctionTypeNode(node) && ts.isTypeAliasDeclaration(node.parent)) return '<call>';
    const name = nameOf(node);
    if (name) return name;
    if (node.parent && (ts.isVariableDeclaration(node.parent) || ts.isPropertyAssignment(node.parent) || ts.isPropertyDeclaration(node.parent))) return nameOf(node.parent) || '<lambda>';
    return '<lambda>';
  }
  function owner(node) {
    const parts = [];
    for (let current = node; current && current !== tree; current = current.parent) {
      if (functionNode(current)) parts.unshift(ownName(current));
      else if (ts.isClassDeclaration(current) || ts.isClassExpression(current) || ts.isInterfaceDeclaration(current) || ts.isTypeAliasDeclaration(current) || ts.isModuleDeclaration(current)) parts.unshift(nameOf(current) || '<class>');
    }
    return parts.join('.') || '<module>';
  }
  function chain(node) {
    node = transparent(node);
    const parts = [];
    while (node && (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node))) {
      const name = ts.isPropertyAccessExpression(node) ? node.name.text : node.argumentExpression && (ts.isStringLiteralLike(node.argumentExpression) || ts.isNumericLiteral(node.argumentExpression)) ? node.argumentExpression.text : undefined;
      if (name === undefined) return undefined;
      parts.unshift(name);
      node = transparent(node.expression);
    }
    return {root: node, parts};
  }
  function reference(node) {
    const access = chain(node);
    if (!access || !ts.isIdentifier(access.root)) return undefined;
    const symbol = checker.getSymbolAtLocation(access.root);
    const binding = symbol && index.bindings.get(symbol);
    if (binding) {
      const members = [binding.member === 'default' ? '' : binding.member, ...access.parts].filter(Boolean);
      const knownTool = ['vitest', '@jest/globals'].includes(binding.spec);
      const canonical = knownTool ? members.join('.') : binding.spec === 'react' ? members.join('.') : [binding.spec.replace(/^node:/, ''), ...members].join('.');
      return {...access, binding, canonical, global: false};
    }
    if (symbol?.declarations?.some(declaration => !declaration.getSourceFile().isDeclarationFile)) return {...access, local: true};
    return {...access, canonical: [access.root.text, ...access.parts].join('.'), global: true};
  }
  function add(node, code, key, message, hint) {
    out.purity.push([line(node), code, key, message, hint]);
  }
  function effect(node, name) {
    add(node, 'impure-call', `${owner(node)}#${name}`, `\`${owner(node)}\` performs an effectful operation \`${name}\``, 'move I/O, timers and stateful hooks to an explicit boundary');
  }
  // meiva:note
  // `process.env.NEXT_PUBLIC_<NAME>` (or the same with a string key, or `process.env?.NEXT_PUBLIC_<NAME>`) in a file the Next.js
  // build bundles is replaced by the value the build saw, provided that variable is set then: the expression is a
  // constant of the build, so it acquires nothing. The root must be the global `process` (an imported one is not replaced),
  // and `process?.env` is not replaced by Turbopack either. Only a read counts as inlined: the target of an assignment,
  // `delete` or `++` stays an environment access, and so does any other way to the environment (`process.env[name]`, a
  // destructuring of `process.env`, a copy of it) and any file the build does not bundle.
  function inlinedEnv(node) {
    if (!config.inlined_env_roots.some(root => out.path.startsWith(root))) return false;
    for (let current = transparent(node); current && (ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current)); current = transparent(current.expression)) {
      const env = transparent(current.expression);
      if (!env || !ts.isPropertyAccessExpression(env) || env.name.text !== 'env' || env.questionDotToken) continue;
      const root = transparent(env.expression);
      const ref = root && ts.isIdentifier(root) ? reference(root) : undefined;
      if (!ref?.global || ref.canonical !== 'process') continue;
      const key = ts.isPropertyAccessExpression(current) ? current.name.text : current.argumentExpression && ts.isStringLiteralLike(current.argumentExpression) ? current.argumentExpression.text : undefined;
      const prefix = config.inlined_env_prefix;
      if (key === undefined || !key.startsWith(prefix) || key.length === prefix.length || !/^\w+$/.test(key)) return false;
      return !written(current);
    }
    return false;
  }
  // meiva:note
  // The node with the parentheses and assertions around it (`(process.env as X)`): what the code means by the value.
  function climbed(node) {
    let top = node;
    while (top.parent && (ts.isParenthesizedExpression(top.parent) || ts.isAsExpression(top.parent) || ts.isNonNullExpression(top.parent) || ts.isSatisfiesExpression(top.parent) || ts.isTypeAssertionExpression(top.parent))) top = top.parent;
    return top;
  }
  // meiva:note
  // Whether a longer access chain continues from `node` (`(process.env).KEY`): the outer access is the one that acquires.
  function insideAccess(node) {
    const top = climbed(node);
    const parent = top.parent;
    return Boolean(parent && (ts.isPropertyAccessExpression(parent) || ts.isElementAccessExpression(parent)) && parent.expression === top);
  }
  // meiva:note
  // Whether the value `node` is written to or removed rather than read: the left side of an assignment (plain or compound),
  // the operand of `delete`, `++` or `--`, however many parentheses or assertions surround it.
  function written(node) {
    const target = climbed(node);
    const parent = target.parent;
    if (!parent) return false;
    if (ts.isBinaryExpression(parent)) return parent.left === target && parent.operatorToken.kind >= ts.SyntaxKind.FirstAssignment && parent.operatorToken.kind <= ts.SyntaxKind.LastAssignment;
    if (ts.isDeleteExpression(parent)) return true;
    return (ts.isPrefixUnaryExpression(parent) || ts.isPostfixUnaryExpression(parent)) && [ts.SyntaxKind.PlusPlusToken, ts.SyntaxKind.MinusMinusToken].includes(parent.operator);
  }
  // meiva:note
  // A browser global named on its own as a value (`const store = localStorage`, `f(location)`): the symbol is a variable or a
  // function of the library, not a property, and the use is neither a feature test (`typeof location`) nor a type.
  function bareBrowserGlobal(node) {
    const symbol = checker.getSymbolAtLocation(node);
    const parent = node.parent;
    if (!symbol || !(symbol.flags & (ts.SymbolFlags.Variable | ts.SymbolFlags.Function))) return false;
    return !(parent && (ts.isTypeOfExpression(parent) || ts.isTypeQueryNode(parent) || (ts.isPropertyAccessExpression(parent) && parent.name === node)));
  }
  function acquisition(node, name, called) {
    if (test) return;
    const ref = reference(node);
    const qualified = ref?.binding?.firstParty ? [ref.binding.target.replace(/\.(?:[cm]?ts|tsx)$/, '').replaceAll('/', '.'), ref.binding.member, ...ref.parts].filter(Boolean).join('.') : name;
    const source = called && ambientPolicy.calls[qualified] ? qualified : Object.keys(ambientPolicy.names).find(target => qualified === target || qualified.startsWith(target + '.'));
    const configured = source && ((called && ambientPolicy.calls[source]) || ambientPolicy.names[source]);
    if (configured) { out.ambient.push([line(node), configured, source]); return; }
    const reads = {'process.env': 'environ', 'process.pid': 'pid', 'process.ppid': 'pid', 'process.platform': 'platform', 'process.arch': 'platform', ...((ref ?? readable(node))?.global ? browserReads : {})};
    const calls = {'Date.now': 'clock', 'Date': 'clock', 'Math.random': 'random', 'crypto.randomUUID': 'random', 'crypto.getRandomValues': 'random', 'crypto.randomBytes': 'random', 'crypto.randomInt': 'random', 'process.cwd': 'cwd', 'os.homedir': 'home', 'os.platform': 'platform', 'os.hostname': 'platform', 'process.getuid': 'pid', 'process.getgid': 'pid'};
    const target = Object.keys(reads).find(key => name === key || name.startsWith(key + '.'));
    const kind = target ? reads[target] : called ? calls[name] : undefined;
    if (kind && !(target === 'process.env' && inlinedEnv(node))) out.ambient.push([line(node), kind, target ?? name]);
  }
  // meiva:note
  // The longest part of an access chain that names a global or an import, for `process.env[name]` and `localStorage[key].x`,
  // where the chain breaks at a computed key and `reference` finds nothing.
  function readable(node) {
    let current = transparent(node);
    while (current) {
      const ref = reference(current);
      if (ref?.canonical) return ref;
      current = ts.isPropertyAccessExpression(current) || ts.isElementAccessExpression(current) ? transparent(current.expression) : undefined;
    }
    return undefined;
  }
  function patch(node, ref) {
    if (!test || !ref?.canonical) return;
    const form = ref.canonical;
    if (!/^(?:vi|jest)\.(?:mock|doMock|spyOn|stubEnv|stubGlobal|replaceProperty)$/.test(form)) return;
    const [first, second] = node.arguments;
    if (form.endsWith('.mock') || form.endsWith('.doMock')) {
      const literal = first && ts.isCallExpression(first) && first.expression.kind === ts.SyntaxKind.ImportKeyword ? first.arguments[0] : first;
      if (!literal || !ts.isStringLiteralLike(literal)) {
        out.patches.push([line(node), form, first?.getText(tree) ?? '<missing>', 'unresolved-patch']);
        return;
      }
      const binding = index.record(literal, node, [], 'patch');
      out.patches.push([line(node), form, binding.target, binding.unresolved ? 'unresolved-patch' : binding.firstParty ? 'first-party-patch' : 'third-party-patch']);
    } else if (form.endsWith('.stubEnv') || form.endsWith('.stubGlobal')) {
      out.patches.push([line(node), form, first && ts.isStringLiteralLike(first) ? first.text : first?.getText(tree) ?? '<missing>', form.endsWith('.stubEnv') ? 'env-patch' : 'stdlib-patch']);
    } else {
      const target = first && reference(first);
      const member = second && ts.isStringLiteralLike(second) ? second.text : undefined;
      const known = target && !target.local && member;
      const label = known ? [target.binding?.target ?? target.root.text, ...target.parts, member].join('.') : [first?.getText(tree), second?.getText(tree)].join('.');
      const code = !known ? 'unresolved-patch' : target.binding ? (target.binding.firstParty ? 'first-party-patch' : 'third-party-patch') : 'stdlib-patch';
      out.patches.push([line(node), form, label, code]);
      if (target?.parts.length) add(node, 'nested-patch-target', label, `patch reaches through nested target \`${label}\``, 'inject the direct collaborator instead of traversing its internals');
    }
  }
  function inspectCall(node) {
    const ref = reference(node.expression);
    if (ts.isCallExpression(node)) patch(node, ref);
    if (!ref?.canonical || ref.local) return;
    const name = ref.canonical;
    const constructing = ts.isNewExpression(node);
    if ((constructing && name === 'Date' && !node.arguments?.length) || impure.has(name) || impure.has(name.split('.')[0]) || (hooks.has(ref.binding ? [...ref.parts].at(-1) ?? ref.binding.member : name) && !ref.local)) effect(node, constructing ? `new ${name}()` : name);
    if (!(constructing && name === 'Date' && node.arguments?.length)) acquisition(node.expression, name, true);
  }
  const hasAny = node => node?.kind === ts.SyntaxKind.AnyKeyword;
  function functionRules(node) {
    const name = owner(node);
    const parameters = node.parameters.filter(parameter => nameOf(parameter) !== 'this');
    if (!test && parameters.length > config.max_params) add(node, 'wide-params', name, `\`${name}\` takes ${parameters.length} parameters (max ${config.max_params})`, 'group related parameters into a cohesive value type');
    if (test) return;
    for (const parameter of parameters) if (hasAny(parameter.type)) out.any.push([line(parameter.type), column(parameter.type), name, nameOf(parameter) || '<pattern>', 'parameter']);
    if (hasAny(node.type)) out.any.push([line(node.type), column(node.type), name, 'return', 'return']);
    if (flags(node) & ts.ModifierFlags.Async) effect(node, 'async');
    const publicFunction = exported(node) || ((ts.isArrowFunction(node) || ts.isFunctionExpression(node)) && ts.isVariableDeclaration(node.parent) && exported(node.parent.parent.parent));
    const untyped = parameters.some(parameter => hasAny(parameter.type)) || hasAny(node.type);
    if (publicFunction && untyped) add(node, 'untyped-export', name, `exported function \`${name}\` exposes any at its boundary`, 'use concrete parameter and return types; compiler-inferred concrete types are accepted');
    if (!publicFunction || parameters.some(parameter => parameter.initializer || parameter.questionToken || parameter.dotDotDotToken || !ts.isIdentifier(parameter.name))) return;
    let body = node.body;
    if (!body) return;
    if (ts.isBlock(body)) body = body.statements.length === 1 && ts.isReturnStatement(body.statements[0]) ? body.statements[0].expression : undefined;
    if (body && ts.isAwaitExpression(body)) body = body.expression;
    if (body && ts.isCallExpression(body) && parameters.length && body.arguments.length === parameters.length && body.arguments.every((argument, i) => ts.isIdentifier(argument) && checker.getSymbolAtLocation(argument) === checker.getSymbolAtLocation(parameters[i].name))) {
      add(node, 'pass-through-function', name, `\`${name}\` only forwards its parameters`, 'call the target directly or give the wrapper a domain responsibility');
    }
  }
  function mutableInitializer(original) {
    const node = transparent(original);
    if (!node) return false;
    const allocation = ts.isCallExpression(node) && reference(node.expression)?.canonical === 'Object.freeze' ? transparent(node.arguments[0]) : node;
    return allocation && ts.isNewExpression(allocation) && ['Map', 'Set', 'WeakMap', 'WeakSet'].includes(reference(allocation.expression)?.canonical);
  }
  function moduleVariable(node) {
    const statement = node.parent.parent;
    if (!ts.isVariableStatement(statement) || statement.parent !== tree) return;
    const name = nameOf(node);
    if (!name) return;
    out.defined.push(name);
    const value = node.initializer && transparent(node.initializer);
    if (value && functionNode(value)) out.symbols.push([name, line(statement), tree.getLineAndCharacterOfPosition(statement.end).line + 1]);
    if (!test && (!(node.parent.flags & ts.NodeFlags.Const) || mutableInitializer(node.initializer))) add(node, 'unfrozen-value', name, `module binding \`${name}\` owns mutable state`, 'use an immutable value or move state into an explicit owner');
  }
  function write(node, target) {
    const access = chain(target);
    if (!test && access?.parts.length >= 2) add(node, 'nested-attr-write', target.getText(tree), `write reaches through \`${target.getText(tree)}\``, 'ask the immediate owner to update its state');
    const ref = reference(target);
    if (ref?.binding && ref.parts.length) out.patches.push([line(node), 'assignment', [ref.binding.target, ...ref.parts].join('.'), ref.binding.firstParty ? 'first-party-patch' : 'third-party-patch']);
  }
  function subset(node) {
    const values = [];
    function collect(expression) {
      expression = transparent(expression);
      if (ts.isBinaryExpression(expression) && expression.operatorToken.kind === ts.SyntaxKind.BarBarToken) { collect(expression.left); collect(expression.right); }
      else if (ts.isBinaryExpression(expression) && [ts.SyntaxKind.EqualsEqualsEqualsToken, ts.SyntaxKind.EqualsEqualsToken].includes(expression.operatorToken.kind)) values.push(expression.right);
    }
    if (ts.isIfStatement(node)) collect(node.expression);
    else if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === 'includes' && ts.isArrayLiteralExpression(node.expression.expression)) values.push(...node.expression.expression.elements);
    const groups = new Map();
    for (const value of values) {
      if (!ts.isPropertyAccessExpression(value)) continue;
      let symbol = checker.getSymbolAtLocation(value.expression);
      if (symbol?.flags & ts.SymbolFlags.Alias) symbol = checker.getAliasedSymbol(symbol);
      const declaration = symbol?.declarations?.find(ts.isEnumDeclaration);
      if (!declaration || declaration.getSourceFile() === tree) continue;
      const identity = `${context.relative(declaration.getSourceFile().fileName)}.${declaration.name.text}`;
      const members = groups.get(identity) ?? new Set(); members.add(value.name.text); groups.set(identity, members);
    }
    for (const [identity, members] of groups) if (members.size > 1) out.subsets.push([line(node), identity, [...members].sort()]);
  }
  function visit(node) {
    if (ts.isIdentifier(node)) {
      const ref = reference(node);
      if (ref?.binding?.firstParty) acquisition(node, ref.canonical, false);
      else if (ref?.global && bareBrowserGlobal(node)) acquisition(node, ref.canonical, false);
    }
    if (functionNode(node)) functionRules(node);
    if (ts.isVariableDeclaration(node)) moduleVariable(node);
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) inspectCall(node);
    if (!test && (ts.isAsExpression(node) || ts.isTypeAssertionExpression(node)) && !(ts.isTypeReferenceNode(node.type) && node.type.typeName.getText(tree) === 'const')) out.casts.push([line(node), column(node), owner(node)]);
    if (!test && (ts.isPropertyDeclaration(node) || ts.isPropertySignature(node)) && hasAny(node.type)) out.any.push([line(node.type), column(node.type), owner(node), nameOf(node), 'class-field']);
    if (ts.isBinaryExpression(node) && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment) write(node, node.left);
    if ((ts.isPrefixUnaryExpression(node) || ts.isPostfixUnaryExpression(node)) && [ts.SyntaxKind.PlusPlusToken, ts.SyntaxKind.MinusMinusToken].includes(node.operator)) write(node, node.operand);
    if (ts.isDeleteExpression(node)) write(node, node.expression);
    if (ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      const outer = node.parent && (ts.isPropertyAccessExpression(node.parent) || ts.isElementAccessExpression(node.parent)) && node.parent.expression === node;
      if (!outer) {
        const ref = reference(node);
        if (ref?.binding?.firstParty && ref.binding.member === '' && ref.parts.length && ref.binding.target !== out.path) {
          out.facts.push([ref.binding.target, line(node), 'attribute', [ref.parts[0]], node.getText(tree)]);
        }
        if (ref?.canonical) {
          if (!insideAccess(node)) acquisition(node, ref.canonical, false);
          if (ref.global && impure.has(ref.root.text) && !ts.isCallExpression(node.parent)) effect(node, ref.root.text);
        } else {
          const prefix = readable(node);
          if (prefix && !insideAccess(node)) acquisition(node, prefix.canonical, false);
        }
        const access = chain(node);
        const hops = access ? access.parts.length - (ref?.binding?.member === '' ? 1 : 0) : 0;
        if (!test && hops > config.max_attr_hops) add(node, 'deep-attr-chain', node.getText(tree), `attribute chain has ${hops} non-module hops (max ${config.max_attr_hops})`, 'pass the needed value or ask the immediate collaborator');
      }
    }
    if (!test) subset(node);
    ts.forEachChild(node, visit);
  }
  for (const statement of tree.statements) {
    if ((ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement)) && statement.name) {
      out.defined.push(statement.name.text);
      out.symbols.push([statement.name.text, line(statement), tree.getLineAndCharacterOfPosition(statement.end).line + 1]);
    }
    if (!test && ts.isClassDeclaration(statement) && !nameOf(statement).endsWith('Error') && !statement.members.some(member => ts.isMethodDeclaration(member) || ts.isGetAccessorDeclaration(member) || ts.isSetAccessorDeclaration(member))) {
      const mutable = statement.members.some(member => ts.isPropertyDeclaration(member) && !(flags(member) & ts.ModifierFlags.Readonly)) || statement.members.some(member => ts.isConstructorDeclaration(member) && member.parameters.some(parameter => (flags(parameter) & (ts.ModifierFlags.Public | ts.ModifierFlags.Private | ts.ModifierFlags.Protected)) && !(flags(parameter) & ts.ModifierFlags.Readonly)));
      if (mutable) add(statement, 'unfrozen-value', nameOf(statement), `class \`${nameOf(statement)}\` has mutable fields`, 'use readonly fields for a value class or an explicit state owner');
    }
  }
  let behavioral = 0;
  const moduleSymbol = checker.getSymbolAtLocation(tree);
  if (moduleSymbol) for (let symbol of checker.getExportsOfModule(moduleSymbol)) {
    const name = symbol.name;
    if (symbol.flags & ts.SymbolFlags.Alias) symbol = checker.getAliasedSymbol(symbol);
    if (symbol.flags & ts.SymbolFlags.Value) out.exports.push(name);
    if (symbol.declarations?.some(declaration => declaration.getSourceFile() === tree && (
      ts.isFunctionDeclaration(declaration) ||
      (ts.isClassDeclaration(declaration) && !name.endsWith('Error') && declaration.members.some(ts.isMethodDeclaration)) ||
      (ts.isVariableDeclaration(declaration) && declaration.initializer && functionNode(transparent(declaration.initializer)))
    ))) behavioral++;

  }
  visit(tree);
  if (!test && !['index.ts', 'index.tsx', 'index.mts', 'index.cts'].includes(out.path.split('/').at(-1)) && behavioral > config.max_public_symbols) add(tree, 'wide-public-surface', '__all__', `${out.path} exports ${behavioral} behavioral symbols (max ${config.max_public_symbols})`, 'split cohesive responsibilities or narrow the exported surface');
}
