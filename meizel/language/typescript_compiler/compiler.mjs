// meiva:note
// Both ordinary checking and optional source evidence use this tool-owned compiler installation.
// meiva:link meizel/docs/checker/typescript.meiva.okf.md
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

function lockedTypescriptVersion(lockText, pkgDir = '') {
  if (!/^lockfileVersion:\s*['"]?9\.0['"]?\s*$/m.test(lockText)) {
    throw new Error(`invalid pnpm-lock.yaml header in ${pkgDir || '.'}`);
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
  throw new Error(`missing typescript version in ${pkgDir ? `${pkgDir}/` : ''}pnpm-lock.yaml`);
}

function findPackageDir(toolRoot) {
  if (process.env.MEIZEL_TS_PACKAGE_DIR) {
    return process.env.MEIZEL_TS_PACKAGE_DIR;
  }
  const policyPath = path.join(toolRoot, 'meizel.policy.toml');
  if (fs.existsSync(policyPath)) {
    const text = fs.readFileSync(policyPath, 'utf8');
    const match = text.match(/package-dir\s*=\s*\[\s*["']([^"']+)["']/);
    if (match) return match[1];
  }
  try {
    for (const entry of fs.readdirSync(toolRoot, { withFileTypes: true })) {
      if (entry.isDirectory() && !entry.name.startsWith('.') && entry.name !== 'node_modules') {
        if (fs.existsSync(path.join(toolRoot, entry.name, 'pnpm-lock.yaml'))) {
          return entry.name;
        }
        try {
          for (const sub of fs.readdirSync(path.join(toolRoot, entry.name), { withFileTypes: true })) {
            if (sub.isDirectory() && fs.existsSync(path.join(toolRoot, entry.name, sub.name, 'pnpm-lock.yaml'))) {
              return `${entry.name}/${sub.name}`;
            }
          }
        } catch {}
      }
    }
  } catch {}
  return '';
}

export const tool = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
export const packageDir = findPackageDir(tool);
const packageJsonPath = packageDir ? path.join(tool, packageDir, 'package.json') : path.join(tool, 'package.json');
const require = createRequire(packageJsonPath);
export const ts = require('typescript');
export const compiler = fs.realpathSync(require.resolve('typescript'));
const expected = packageDir
  ? path.join(tool, packageDir, 'node_modules/typescript/lib/typescript.js')
  : path.join(tool, 'node_modules/typescript/lib/typescript.js');
if (compiler !== fs.realpathSync(expected)) throw new Error('unexpected TypeScript compiler import path');
const lockPath = packageDir ? path.join(tool, packageDir, 'pnpm-lock.yaml') : path.join(tool, 'pnpm-lock.yaml');
const packageLockInstalled = packageDir
  ? path.join(tool, packageDir, 'node_modules/typescript/package.json')
  : path.join(tool, 'node_modules/typescript/package.json');
const lockedVersion = lockedTypescriptVersion(fs.readFileSync(lockPath, 'utf8'), packageDir);
const installed = JSON.parse(fs.readFileSync(packageLockInstalled, 'utf8'));
if (ts.version !== installed.version || ts.version !== lockedVersion) {
  throw new Error(`TypeScript compiler differs from the tool lock; run pnpm install --frozen-lockfile in ${packageDir || '.'}`);
}
