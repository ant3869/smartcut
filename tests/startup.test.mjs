import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readdirSync, readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

// app.js ships as <script type="module">, so it must parse with the module
// goal. Plain `node --check file.js` parses .js as CommonJS and happily
// passes a module with an unclosed brace (blank black window at startup,
// console: "Unexpected end of input"). Parse every frontend script the way
// the browser will.
const frontend = join(dirname(fileURLToPath(import.meta.url)), '..', 'frontend');
const scripts = readdirSync(frontend)
  .filter(f => f.endsWith('.mjs') || f === 'app.js')
  .map(f => join(frontend, f));

test('frontend scripts parse as ES modules', () => {
  assert.ok(scripts.length > 0, 'expected frontend scripts, found none');
  for (const script of scripts) {
    try {
      execFileSync(process.execPath, ['--input-type=module', '--check'],
        {input: readFileSync(script), stdio: ['pipe', 'ignore', 'pipe']});
    } catch (e) {
      assert.fail(`${script} fails module parse: ${(e.stderr || '').toString().split('\n')[0]}`);
    }
  }
});
