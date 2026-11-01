// meiva:note
// Both ordinary checking and optional source evidence use this tool-owned compiler installation.
// meiva:link meizel/docs/checker/typescript.meiva.okf.md
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

function lockedTypescriptVersion(lockText) {
  if (!/^lockfileVersion:\s*['"]?9\.0['"]?\s*$/m.test(lockText)) {
    throw new Error('invalid pnpm-lock.yaml header in ui/web');
  }
  let scope = ['', '', '', ''];
  for (const rawLine of lockText.split(/\r?\n/)) {
    if (!rawLine.trim() || rawLine.trimStart().startsWith('#')) continue;
    const indent = rawLine.length - rawLine.replace(/^ +/, '').length;
    const trimmed = rawLine.trim();
    const colon = trimmed.indexOf(':');
    if (colon === -1 || indent % 2 !== 0) continue;
    const key = trimmed.slice(0, colon).trim().replace(/^['"]|['"]$/g, '');
    const rest = trimmed.slice(colon + 1).trim().replace(/^['"]|['"]$/g, '');
    const level = indent / 2;
    if (level === 0 && scope[0] === 'importers') break;
    if (level < 4) {
      scope = [...scope.slice(0, level), key, ...Array(3 - level).fill('')];
    } else if (
      level === 4 &&
      scope[0] === 'importers' &&
      scope[1] === '.' &&
      (scope[2] === 'dependencies' || scope[2] === 'devDependencies') &&
      scope[3] === 'typescript' &&
      key === 'version'
    ) {
      const version = rest.split('(')[0].trim();
      if (!version) break;
      return version;
    }
  }
  throw new Error('missing typescript version in ui/web/pnpm-lock.yaml');
}

export const tool = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const require = createRequire(path.join(tool, 'ui/web/package.json'));
export const ts = require('typescript');
export const compiler = fs.realpathSync(require.resolve('typescript'));
const expected = path.join(tool, 'ui/web/node_modules/typescript/lib/typescript.js');
if (compiler !== fs.realpathSync(expected)) throw new Error('unexpected TypeScript compiler import path');
const lockedVersion = lockedTypescriptVersion(fs.readFileSync(path.join(tool, 'ui/web/pnpm-lock.yaml'), 'utf8'));
const installed = JSON.parse(fs.readFileSync(path.join(tool, 'ui/web/node_modules/typescript/package.json'), 'utf8'));
if (ts.version !== installed.version || ts.version !== lockedVersion) {
  throw new Error('TypeScript compiler differs from the tool lock; run pnpm install --frozen-lockfile in ui/web');
}
