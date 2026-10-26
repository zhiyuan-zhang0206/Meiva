// meiva:note
// One compiler context owns lexical binding; each importing file's project options own resolution.
// meiva:link meizel/docs/checker/typescript.meiva.okf.md
import fs from 'node:fs';
import path from 'node:path';

export function project(ts, input) {
  const root = path.resolve(input.root);
  const absolute = relative => path.resolve(root, relative);
  const relative = file => path.relative(root, file).split(path.sep).join('/');
  const tracked = new Set(input.files);
  const provided = new Map(Object.entries(input.sources).map(([file, text]) => [absolute(file), text]));
  const directories = new Set([root]);
  for (const file of [...input.files, ...Object.keys(input.sources)]) {
    let directory = path.dirname(absolute(file));
    while (directory === root || directory.startsWith(root + path.sep)) {
      directories.add(directory);
      if (directory === root) break;
      directory = path.dirname(directory);
    }
  }
  const optionsCache = new Map();
  const extendedConfigCache = new Map();
  const resolutionCache = new Map();
  const packages = new Set();
  for (const file of input.files.filter(file => file.endsWith('package.json'))) {
    const full = absolute(file);
    if (fs.existsSync(full)) {
      const manifest = JSON.parse(fs.readFileSync(full, 'utf8'));
      if (typeof manifest.name === 'string') packages.add(manifest.name);
    }
  }
  const host = {
    ...ts.sys,
    directoryExists: directory => directories.has(path.resolve(directory)) || ts.sys.directoryExists(directory),
    fileExists: file => provided.has(path.resolve(file)) || tracked.has(relative(file)) || ts.sys.fileExists(file),
    readFile: file => provided.get(path.resolve(file)) ?? ts.sys.readFile(file),
  };
  function optionsFor(file) {
    let dir = path.dirname(file), config;
    while (dir === root || dir.startsWith(root + path.sep)) {
      const candidate = path.join(dir, 'tsconfig.json');
      if (host.fileExists(candidate)) { config = candidate; break; }
      if (dir === root) break;
      dir = path.dirname(dir);
    }
    const key = config ?? '<default>';
    if (!optionsCache.has(key)) {
      let options = {target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler, jsx: ts.JsxEmit.Preserve};
      if (config) {
        const read = ts.readConfigFile(config, host.readFile);
        if (read.error) throw new Error(ts.flattenDiagnosticMessageText(read.error.messageText, '\n'));
        const parsed = ts.parseJsonConfigFileContent(read.config, host, path.dirname(config), {}, config, undefined, undefined, extendedConfigCache);
        const errors = parsed.errors.filter(error => ![18002, 18003].includes(error.code));
        if (errors.length) throw new Error(`${relative(config)}: ${errors.map(error => ts.flattenDiagnosticMessageText(error.messageText, '\n')).join('; ')}`);
        options = {...options, ...parsed.options};
      }
      const paths = {...options.paths};
      for (const [pattern, target] of input.config.aliases) {
        paths[pattern.endsWith('/') ? pattern + '*' : pattern] = [absolute(target + (pattern.endsWith('/') ? '*' : ''))];
      }
      if (Object.keys(paths).length) options.paths = paths;
      optionsCache.set(key, options);
    }
    return optionsCache.get(key);
  }
  function resolve(specifier, importer) {
    const clean = specifier.split(/[?#]/, 1)[0];
    const options = optionsFor(importer);
    let cache = resolutionCache.get(options);
    if (!cache) {
      cache = ts.createModuleResolutionCache(root, name => name, options);
      resolutionCache.set(options, cache);
    }
    const module = ts.resolveModuleName(clean, importer, options, host, cache).resolvedModule;
    if (module) return module;
    // meiva:note
    // Non-code assets are still dependency facts, and cannot be compiler source files.
    if (clean.startsWith('.')) {
      const candidate = path.resolve(path.dirname(importer), clean);
      if (tracked.has(relative(candidate))) return {resolvedFileName: candidate, extension: ts.Extension.Js};
    }
    return undefined;
  }
  const programOptions = {...optionsFor(absolute(Object.keys(input.sources)[0])), noEmit: true, noResolve: true, types: [], skipLibCheck: true};
  const compilerHost = ts.createCompilerHost(programOptions);
  compilerHost.fileExists = host.fileExists;
  compilerHost.directoryExists = host.directoryExists;
  compilerHost.readFile = host.readFile;
  compilerHost.getSourceFile = (file, version) => {
    const text = host.readFile(file);
    return text === undefined ? undefined : ts.createSourceFile(file, text, version, true);
  };
  compilerHost.resolveModuleNameLiterals = (literals, containingFile) => literals.map(literal => ({resolvedModule: resolve(literal.text, containingFile)}));
  const program = ts.createProgram([...provided.keys()], programOptions, compilerHost);
  const checker = program.getTypeChecker();
  function localSpecifier(spec, importer) {
    if (spec.startsWith('.')) return true;
    if ([...packages].some(name => spec === name || spec.startsWith(name + '/'))) return true;
    return Object.keys(optionsFor(importer).paths ?? {}).some(pattern => {
      const [prefix, suffix] = pattern.split('*');
      return suffix === undefined ? spec === pattern : spec.startsWith(prefix) && spec.endsWith(suffix);
    });
  }
  return {program, checker, tracked, absolute, relative, resolve, localSpecifier};
}
