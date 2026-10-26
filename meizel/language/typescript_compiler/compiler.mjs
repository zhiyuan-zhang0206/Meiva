// meiva:note
// Both ordinary checking and optional source evidence use this tool-owned compiler installation.
// meiva:link meizel/docs/checker/typescript.meiva.okf.md
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

export const tool = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const require = createRequire(path.join(tool, 'ui/web/package.json'));
export const ts = require('typescript');
export const compiler = fs.realpathSync(require.resolve('typescript'));
const expected = path.join(tool, 'ui/web/node_modules/typescript/lib/typescript.js');
if (compiler !== fs.realpathSync(expected)) throw new Error('unexpected TypeScript compiler import path');
const lock = JSON.parse(fs.readFileSync(path.join(tool, 'ui/web/package-lock.json'), 'utf8'));
const installed = JSON.parse(fs.readFileSync(path.join(tool, 'ui/web/node_modules/typescript/package.json'), 'utf8'));
if (ts.version !== installed.version || ts.version !== lock.packages['node_modules/typescript'].version) {
  throw new Error('TypeScript compiler differs from the tool lock; run npm ci in ui/web');
}
