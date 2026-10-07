/* Cleanup script builder for "On disk but not in Plex": pure functions, no DOM.
   Reclaim can't touch these files itself (the media is mounted read-only, or only another
   machine can reach it), so it writes a script you read and run where the files live. */
(function (root) {
  'use strict';

  const SHELLS = {
    bash: { label: 'bash (Unraid, Linux, macOS)', ext: 'sh', sep: '/' },
    powershell: { label: 'PowerShell (Windows share)', ext: 'ps1', sep: '\\' },
  };

  function fmtBytes(b) {
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
    return `${i ? b.toFixed(b < 10 ? 2 : b < 100 ? 1 : 0) : b} ${u[i]}`;
  }

  // single-quoted literals: nothing inside expands in either shell
  const quote = {
    bash: s => `'${s.replace(/'/g, `'\\''`)}'`,
    powershell: s => `'${s.replace(/['‘’‚‛]/g, m => m + m)}'`,
  };
  // a comment must not end early or smuggle in a line
  const oneLine = s => String(s).replace(/[\r\n]+/g, ' ');

  /** Plex path -> path where the script runs, by the longest matching prefix. */
  function mapPath(p, map, sep) {
    let best = null;
    for (const [from, to] of Object.entries(map)) {
      const f = from.replace(/\/+$/, '');
      if (!to || !String(to).trim()) continue;
      if ((p === f || p.startsWith(f + '/')) && (!best || f.length > best.f.length)) best = { f, to: String(to).trim() };
    }
    if (!best) return null;
    const rest = p.slice(best.f.length);
    return best.to.replace(/[\\/]+$/, '') + (sep === '/' ? rest : rest.replace(/\//g, '\\'));
  }

  /**
   * opts: { shell: 'bash'|'powershell', action: 'delete'|'move', files: [{path, size, cat, folder, root}],
   *         map: {plexPrefix: targetPrefix}, holding: target-side folder (move), walkAt, now }
   * folder = the top-level title folder the file was listed under; root = its library root.
   * Returns { text, filename, count, bytes, unmapped: [plex paths], problems: [strings] }.
   */
  function build(opts) {
    const shell = opts.shell, sh = SHELLS[shell], q = quote[shell];
    if (!sh) throw new Error(`unknown shell ${shell}`);
    const problems = [], unmapped = [];
    const files = [];
    for (const f of opts.files) {
      const target = mapPath(f.path, opts.map || {}, sh.sep);
      if (target == null) { unmapped.push(f.path); continue; }
      files.push({ ...f, target });
    }
    const roots = [...new Set(opts.files.map(f => f.root.replace(/\/+$/, '')))];
    const mappedRoots = roots.map(r => mapPath(r, opts.map || {}, sh.sep)).filter(Boolean);
    let holding = null;
    if (opts.action === 'move') {
      holding = (opts.holding || '').trim().replace(/[\\/]+$/, '');
      if (!holding) problems.push('Pick a holding folder to move the files into.');
      const norm = s => s.toLowerCase().replace(/\\/g, '/');
      if (holding && mappedRoots.some(r => norm(holding) === norm(r) || norm(holding).startsWith(norm(r) + '/'))) {
        problems.push('The holding folder is inside a Plex library folder, so Plex would scan the files back in.');
      }
    }
    const bytes = files.reduce((s, f) => s + f.size, 0);
    // folders that may be empty afterwards: each file's parents up to its title folder, deepest first
    const dirs = new Set();
    for (const f of files) {
      const top = f.folder === f.path ? null : f.folder;          // a loose file in the library root has no folder of its own
      if (!top) continue;
      let d = f.path.slice(0, f.path.lastIndexOf('/'));
      while (d.length >= top.length) {
        dirs.add(d);
        d = d.slice(0, d.lastIndexOf('/'));
      }
    }
    const dirList = [...dirs].sort((a, b) => b.split('/').length - a.split('/').length || b.localeCompare(a))
      .map(d => mapPath(d, opts.map || {}, sh.sep)).filter(Boolean);
    const rel = f => {
      const r = f.path.slice(f.root.replace(/\/+$/, '').lastIndexOf('/') + 1);   // keep the library folder name: Movies/Title/file
      return sh.sep === '/' ? r : r.replace(/\//g, '\\');
    };
    const stamp = new Date((opts.now || Date.now())).toISOString().slice(0, 16).replace('T', ' ');
    const walked = opts.walkAt ? new Date(opts.walkAt * 1000).toISOString().slice(0, 16).replace('T', ' ') : 'unknown';
    const verb = opts.action === 'move' ? 'move' : 'delete';
    const summary = `${files.length} file${files.length === 1 ? '' : 's'} (${fmtBytes(bytes)})`;
    const head = [
      `reclaim: ${verb} ${summary} that Plex doesn't index${holding ? ` into ${oneLine(holding)}` : ''}.`,
      `Built ${stamp} UTC from the disk walk of ${walked} UTC. Read the list before running it.`,
    ];
    const name = `reclaim-${verb}-${stamp.slice(0, 10)}.${sh.ext}`;
    const lines = [];
    if (shell === 'bash') {
      lines.push('#!/usr/bin/env bash', ...head.map(l => `# ${l}`), `# Run:  bash ${name}`, 'set -u', '');
      if (verb === 'delete') {
        lines.push('files=(');
        for (const f of files) lines.push(`  ${q(f.target)}   # ${fmtBytes(f.size)} ${f.cat}`);
        lines.push(')');
      } else {
        lines.push(`holding=${q(holding || '')}`, 'moves=(   # source, then its place under the holding folder');
        for (const f of files) lines.push(`  ${q(f.target)} ${q(rel(f))}`);
        lines.push(')');
      }
      lines.push('dirs=(   # removed afterwards only if empty, deepest first');
      for (const d of dirList) lines.push(`  ${q(d)}`);
      lines.push(')', '');
      if (verb === 'delete') {
        lines.push('printf \'%s\\n\' "${files[@]}"',
          `read -r -p "Delete these \${#files[@]} files (${fmtBytes(bytes)})? Type yes: " answer`,
          '[ "$answer" = yes ] || { echo "Nothing deleted."; exit 1; }',
          'done_n=0; gone=0; failed=0',
          'for f in "${files[@]}"; do',
          '  if [ ! -e "$f" ]; then echo "already gone: $f"; gone=$((gone + 1))',
          '  elif rm -f -- "$f"; then done_n=$((done_n + 1))',
          '  else failed=$((failed + 1)); fi',
          'done');
      } else {
        lines.push('for ((i = 0; i < ${#moves[@]}; i += 2)); do echo "${moves[i]}"; done',
          `read -r -p "Move these $((\${#moves[@]} / 2)) files (${fmtBytes(bytes)}) into $holding? Type yes: " answer`,
          '[ "$answer" = yes ] || { echo "Nothing moved."; exit 1; }',
          'done_n=0; gone=0; failed=0',
          'for ((i = 0; i < ${#moves[@]}; i += 2)); do',
          '  src=${moves[i]}; dst=$holding/${moves[i + 1]}',
          '  if [ ! -e "$src" ]; then echo "already gone: $src"; gone=$((gone + 1))',
          '  elif [ -e "$dst" ]; then echo "already in the holding folder, left alone: $dst"; failed=$((failed + 1))',
          '  elif mkdir -p -- "$(dirname -- "$dst")" && mv -n -- "$src" "$dst"; then done_n=$((done_n + 1))',
          '  else failed=$((failed + 1)); fi',
          'done');
      }
      // ${dirs[@]+...}: an empty array under set -u is an error in bash before 4.4 (macOS ships 3.2)
      lines.push('for d in ${dirs[@]+"${dirs[@]}"}; do rmdir -- "$d" 2>/dev/null && echo "removed empty folder: $d"; done',
        `echo "${verb === 'move' ? 'Moved' : 'Deleted'} $done_n, already gone $gone, failed $failed. Walk the disk again so reclaim catches up."`, '');
    } else {
      lines.push(...head.map(l => `# ${l}`), `# Run:  powershell -ExecutionPolicy Bypass -File .\\${name}`, '');
      if (verb === 'delete') {
        lines.push('$files = @(');
        for (const f of files) lines.push(`  ${q(f.target)}   # ${fmtBytes(f.size)} ${f.cat}`);
        lines.push(')');
      } else {
        lines.push(`$holding = ${q(holding || '')}`, '$moves = @(   # source, then its place under the holding folder');
        for (const f of files) lines.push(`  ,@(${q(f.target)}, ${q(rel(f))})`);
        lines.push(')');
      }
      lines.push('$dirs = @(   # removed afterwards only if empty, deepest first');
      for (const d of dirList) lines.push(`  ${q(d)}`);
      lines.push(')', '$done = 0; $gone = 0; $failed = 0', '');
      if (verb === 'delete') {
        lines.push('$files',
          `if ((Read-Host "Delete these $($files.Count) files (${fmtBytes(bytes)})? Type yes") -ne 'yes') { 'Nothing deleted.'; exit 1 }`,
          'foreach ($f in $files) {',
          '  if (-not (Test-Path -LiteralPath $f)) { "already gone: $f"; $gone++ }',
          '  else { try { Remove-Item -LiteralPath $f -Force -ErrorAction Stop; $done++ } catch { "failed: $f ($_)"; $failed++ } }',
          '}');
      } else {
        lines.push('$moves | ForEach-Object { $_[0] }',
          `if ((Read-Host "Move these $($moves.Count) files (${fmtBytes(bytes)}) into $holding? Type yes") -ne 'yes') { 'Nothing moved.'; exit 1 }`,
          'foreach ($m in $moves) {',
          '  $src = $m[0]; $dst = Join-Path $holding $m[1]',
          '  if (-not (Test-Path -LiteralPath $src)) { "already gone: $src"; $gone++ }',
          '  elseif (Test-Path -LiteralPath $dst) { "already in the holding folder, left alone: $dst"; $failed++ }',
          '  else { try { New-Item -ItemType Directory -Force -Path (Split-Path -LiteralPath $dst) -ErrorAction Stop | Out-Null; Move-Item -LiteralPath $src -Destination $dst -ErrorAction Stop; $done++ } catch { "failed: $src ($_)"; $failed++ } }',
          '}');
      }
      lines.push('foreach ($d in $dirs) { if ((Test-Path -LiteralPath $d) -and -not (Get-ChildItem -LiteralPath $d -Force)) { Remove-Item -LiteralPath $d; "removed empty folder: $d" } }',
        `"${verb === 'move' ? 'Moved' : 'Deleted'} $done, already gone $gone, failed $failed. Walk the disk again so reclaim catches up."`, '');
    }
    return { text: lines.join(shell === 'bash' ? '\n' : '\r\n'), filename: name, count: files.length, bytes, unmapped, problems };
  }

  const api = { build, mapPath, quote, SHELLS, fmtBytes };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Cleanup = api;
})(typeof window !== 'undefined' ? window : globalThis);
