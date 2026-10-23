// meiva:note
// Explicit one-process producer: sources and locked compiler inputs are hashed, never evaluated.
// meiva:link meizel/docs/checker/typescript-evidence.meiva.okf.md

import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {sourceEvidence} from './extract.mjs';

const entry = fileURLToPath(import.meta.url);
const tool = path.resolve(path.dirname(entry),'../../..');
const require = createRequire(path.join(tool,'ui/web/package.json'));
const ts = require('typescript');
const compiler = fs.realpathSync(require.resolve('typescript'));
const libraryRoot = path.dirname(compiler);
const digest = data => createHash('sha256').update(data).digest('hex');
const text = file => new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(fs.readFileSync(file));
const inside = (root,file) => file === root || file.startsWith(root+path.sep);

function main(args) {
  if (args[0] !== '--repo' || !args[1] || args.length < 3) throw new Error('Usage: node producer.mjs --repo ROOT FILE.ts [FILE.tsx ...]');
  const root = fs.realpathSync(args[1]);
  if (compiler !== fs.realpathSync(path.join(tool,'ui/web/node_modules/typescript/lib/typescript.js'))) throw new Error('unexpected compiler import path');
  const lock = JSON.parse(text(path.join(tool,'ui/web/package-lock.json')));
  const installed = JSON.parse(text(path.join(tool,'ui/web/node_modules/typescript/package.json')));
  if (ts.version !== installed.version || ts.version !== lock.packages['node_modules/typescript'].version) throw new Error('compiler differs from locked installation');
  const files = new Map();
  for (const relative of args.slice(2)) {
    if (relative.includes('\\') || path.posix.isAbsolute(relative) || path.posix.normalize(relative) !== relative || relative.split('/').includes('..') || !/\.tsx?$/.test(relative) || relative.endsWith('.d.ts')) throw new Error(`invalid source path: ${relative}`);
    const file = fs.realpathSync(path.join(root,relative));
    if (!inside(root,file) || files.has(file)) throw new Error(`outside or duplicate source: ${relative}`);
    files.set(file,{relative,text:text(file)});
  }
  const options = {target:ts.ScriptTarget.ES2022,noEmit:true,strict:true,types:[],skipLibCheck:true};
  const host = ts.createCompilerHost(options);
  const dependencies = new Map();
  const record = file => {
    const relative = path.relative(tool,file).split(path.sep).join('/');
    if (!inside(tool,fs.realpathSync(file))) throw new Error(`outside tool dependency: ${file}`);
    const content = text(file);
    dependencies.set(relative,{path:relative,sha256:digest(content)});
    return content;
  };
  for (const name of fs.readdirSync(path.dirname(entry)).filter(n=>n.endsWith('.mjs')).sort()) record(path.join(path.dirname(entry),name));
  for (const relative of ['ui/web/package-lock.json','ui/web/node_modules/typescript/package.json','ui/web/node_modules/typescript/lib/typescript.js']) record(path.join(tool,relative));
  host.getSourceFile = (file,version) => {
    const normalized = path.resolve(file);
    const supplied = files.get(normalized);
    if (supplied) return ts.createSourceFile(file,supplied.text,version,true,file.endsWith('.tsx')?ts.ScriptKind.TSX:ts.ScriptKind.TS);
    if (inside(libraryRoot,normalized) && normalized.endsWith('.d.ts') && fs.existsSync(normalized)) return ts.createSourceFile(file,record(normalized),version,true);
    return undefined;
  };
  host.fileExists = file => files.has(path.resolve(file)) || (inside(libraryRoot,path.resolve(file)) && file.endsWith('.d.ts') && fs.existsSync(file));
  host.readFile = file => files.get(path.resolve(file))?.text;
  const program = ts.createProgram([...files.keys()],options,host);
  const checker = program.getTypeChecker();
  const libraries = new Map(program.getSourceFiles().filter(file=>program.isSourceFileDefaultLibrary(file)).map(file=>[file.fileName,path.relative(tool,file.fileName).split(path.sep).join('/')]));
  const sources = [];
  for (const [file,input] of files) {
    const tree = program.getSourceFile(file);
    if (!tree) throw new Error(`missing source tree: ${input.relative}`);
    const errors = tree.parseDiagnostics;
    const unavailable = errors.length || !libraries.size;
    sources.push({path:input.relative,sha256:digest(input.text),status:unavailable?'unavailable':'observed',
      reason:errors.length ? errors.map(d=>ts.flattenDiagnosticMessageText(d.messageText,' ')).join('; ') : !libraries.size ? 'default-library binding context unavailable' : 'bounded SourceFile-direct initializer evidence; other behavior remains unknown',
      observations:unavailable ? [] : sourceEvidence(ts,checker,tree,libraries,name=>path.relative(root,name).split(path.sep).join('/'))});
  }
  process.stdout.write(JSON.stringify({version:1,kind:'meizel-ts-holders',root,compiler_version:ts.version,dependencies:[...dependencies.values()].sort((a,b)=>a.path.localeCompare(b.path)),sources},null,2)+'\n');
}

try {main(process.argv.slice(2));} catch (error) {console.error(String(error));process.exitCode=1;}
