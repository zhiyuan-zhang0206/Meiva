// meiva:note
// The compiler is a tool-owned locked dependency. A failed batch never emits a partial clean result.
// meiva:link meizel/docs/checker/typescript.meiva.okf.md
import fs from 'node:fs';
import {ts} from './compiler.mjs';
import {project} from './project.mjs';
import {imports} from './imports.mjs';
import {semantics} from './semantics.mjs';

function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const context = project(ts, input);
  const tests = new Set(input.tests);
  const sources = [];
  for (const name of Object.keys(input.sources)) {
    const tree = context.program.getSourceFile(context.absolute(name));
    if (!tree) throw new Error(`${name}: compiler did not produce a source tree`);
    if (tree.parseDiagnostics.length) {
      throw new Error(tree.parseDiagnostics.map(error => `${name}:${tree.getLineAndCharacterOfPosition(error.start ?? 0).line + 1}: ${ts.flattenDiagnosticMessageText(error.messageText, '\n')}`).join('\n'));
    }
    const result = {path: name, facts: [], external: [], patches: [], purity: [], problems: [], exports: [], defined: [], ambient: [], casts: [], any: [], subsets: [], symbols: [], restricted: []};
    const index = imports(ts, context, tree, result);
    const full = !input.config.scopes.length || input.config.scopes.some(prefix => name === prefix || name.startsWith(prefix + '/'));
    if (full) semantics(ts, context, tree, result, index, input.config, tests.has(name), input.ambient);
    if (input.config.scopes.length) result.facts = result.facts.filter(fact => input.config.scopes.some(prefix => fact[0] === prefix || fact[0].startsWith(prefix + '/')));
    if (tests.has(name)) result.exports = null;
    for (const key of ['facts', 'external', 'patches', 'purity', 'problems', 'defined', 'ambient', 'casts', 'any', 'subsets', 'symbols', 'restricted']) {
      result[key] = [...new Map(result[key].map(value => [JSON.stringify(value), value])).values()];
    }
    sources.push(result);
  }
  process.stdout.write(JSON.stringify({version: 1, sources}) + '\n');
}
try { main(); } catch (error) { console.error(String(error)); process.exitCode = 1; }
