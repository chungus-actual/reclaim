// Cleanup scripts, end to end: build a fake library, generate each script, run it for real.
//   node tests/test_cleanup.mjs      (bash always; Windows PowerShell too when on Windows)
import { createRequire } from 'node:module';
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const C = require('../backend/static/cleanup.js');

// awkward names on purpose: quotes, smart quotes, brackets, $, spaces, non-ASCII
const TITLE = "Bob's [Cut] $HOME ½ ‘q’ (2012)";
const fixture = () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'reclaim-cleanup-'));
  const lib = path.join(base, 'media');
  const mk = (rel, bytes = 10) => {
    const p = path.join(lib, ...rel.split('/'));
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, Buffer.alloc(bytes));
  };
  mk(`Movies/${TITLE}/${TITLE}.iso`, 300);
  mk(`Movies/${TITLE}/BDMV/STREAM/00001.m2ts`, 200);
  mk(`Movies/${TITLE}/${TITLE}.en.srt`, 5);          // left alone: the folder must survive
  mk('Movies/Gone (2001)/gone.mkv', 50);              // listed, but deleted before the script runs
  mk('TV/loose in root.avi', 70);                     // a loose file directly in the library root
  fs.rmSync(path.join(lib, 'Movies', 'Gone (2001)', 'gone.mkv'));
  return { base, lib };
};
const files = [
  { path: `/data/Movies/${TITLE}/${TITLE}.iso`, size: 300, cat: 'disc', folder: `/data/Movies/${TITLE}`, root: '/data/Movies' },
  { path: `/data/Movies/${TITLE}/BDMV/STREAM/00001.m2ts`, size: 200, cat: 'disc', folder: `/data/Movies/${TITLE}`, root: '/data/Movies' },
  { path: '/data/Movies/Gone (2001)/gone.mkv', size: 50, cat: 'video', folder: '/data/Movies/Gone (2001)', root: '/data/Movies' },
  { path: '/data/TV/loose in root.avi', size: 70, cat: 'video', folder: '/data/TV/loose in root.avi', root: '/data/TV' },
];
const exists = (lib, rel) => fs.existsSync(path.join(lib, ...rel.split('/')));

const shells = [['bash', 'bash', f => ['bash', [f]]]];
if (process.platform === 'win32') shells.push(['powershell', 'powershell', f => ['powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', f]]]);

function run(shell, cmd, text, answer, base) {
  const ext = C.SHELLS[shell].ext;
  const f = path.join(base, `script.${ext}`);
  fs.writeFileSync(f, (shell === 'powershell' ? '﻿' : '') + text);   // PS 5.1 reads BOM-less scripts as ANSI
  const [exe, args] = cmd(f);
  const r = spawnSync(exe, args, { input: answer + '\n', encoding: 'utf8' });
  return (r.stdout || '') + (r.stderr || '');
}

let n = 0;
for (const [shell, , cmd] of shells) {
  const target = lib => shell === 'bash' && process.platform === 'win32' ? lib.replace(/\\/g, '/') : lib;

  // delete, answered "yes"
  let { base, lib } = fixture();
  let s = C.build({ shell, action: 'delete', files, map: { '/data': target(lib) }, walkAt: 1.7e9, now: 1.8e12 });
  assert.equal(s.count, 4); assert.deepEqual(s.unmapped, []); assert.deepEqual(s.problems, []);
  let out = run(shell, cmd, s.text, 'yes', base);
  assert.ok(!exists(lib, `Movies/${TITLE}/${TITLE}.iso`), `${shell}: iso deleted\n${out}`);
  assert.ok(!exists(lib, `Movies/${TITLE}/BDMV`), `${shell}: emptied BDMV tree removed\n${out}`);
  assert.ok(exists(lib, `Movies/${TITLE}/${TITLE}.en.srt`), `${shell}: unselected sidecar kept`);
  assert.ok(!exists(lib, 'TV/loose in root.avi') && exists(lib, 'TV'), `${shell}: root-level file gone, library root kept`);
  assert.ok(!exists(lib, 'Movies/Gone (2001)'), `${shell}: folder of an already-gone file removed once empty`);
  assert.match(out, /Deleted 3, already gone 1, failed 0/, `${shell}: summary\n${out}`);
  fs.rmSync(base, { recursive: true, force: true }); n++;

  // delete, answered anything else: nothing happens
  ({ base, lib } = fixture());
  s = C.build({ shell, action: 'delete', files, map: { '/data': target(lib) } });
  out = run(shell, cmd, s.text, 'no', base);
  assert.ok(exists(lib, `Movies/${TITLE}/${TITLE}.iso`), `${shell}: "no" deletes nothing\n${out}`);
  assert.match(out, /Nothing deleted/);
  fs.rmSync(base, { recursive: true, force: true }); n++;

  // move into a holding folder outside the library
  ({ base, lib } = fixture());
  const hold = path.join(base, 'holding');
  s = C.build({ shell, action: 'move', files, map: { '/data': target(lib) }, holding: target(hold) });
  assert.deepEqual(s.problems, []);
  out = run(shell, cmd, s.text, 'yes', base);
  const held = rel => fs.existsSync(path.join(hold, ...rel.split('/')));
  assert.ok(held(`Movies/${TITLE}/${TITLE}.iso`) && held(`Movies/${TITLE}/BDMV/STREAM/00001.m2ts`) && held('TV/loose in root.avi'),
    `${shell}: moved with library-relative paths\n${out}`);
  assert.ok(!exists(lib, `Movies/${TITLE}/${TITLE}.iso`) && exists(lib, `Movies/${TITLE}/${TITLE}.en.srt`), `${shell}: sources gone, sidecar kept`);
  assert.match(out, /Moved 3, already gone 1, failed 0/, `${shell}: summary\n${out}`);
  // running it again finds nothing to move
  out = run(shell, cmd, s.text, 'yes', base);
  assert.match(out, /Moved 0, already gone 4, failed 0/, `${shell}: rerun\n${out}`);
  fs.rmSync(base, { recursive: true, force: true }); n++;
  console.log('ok', shell);
}

// guards that don't need a shell
const inLib = C.build({ shell: 'bash', action: 'move', files, map: { '/data': '/mnt/user/media' }, holding: '/mnt/user/media/Movies/_hold' });
assert.equal(inLib.problems.length, 1, 'holding folder inside a library is flagged');
const partial = C.build({ shell: 'bash', action: 'delete', files, map: { '/data/Movies': '/m' } });
assert.deepEqual(partial.unmapped, ['/data/TV/loose in root.avi'], 'files without a mapping are reported, not guessed');
assert.equal(C.quote.powershell("a‘b'c"), "'a‘‘b''c'");
assert.equal(C.quote.bash("it's"), `'it'\\''s'`);
console.log(`${n + 1} passed`);
