import test from 'node:test';
import assert from 'node:assert/strict';
import * as Theme from '../frontend/theme.mjs';
import {icon, iconNames} from '../frontend/icons.mjs';

test('auto follows the OS, explicit modes pin, junk falls back to auto', () => {
  assert.equal(Theme.resolve('auto', true), 'light');
  assert.equal(Theme.resolve('auto', false), 'dark');
  assert.equal(Theme.resolve('dark', true), 'dark');
  assert.equal(Theme.resolve('light', false), 'light');
  assert.equal(Theme.resolve('sepia', true), 'light');
  assert.equal(Theme.resolve(null, false), 'dark');
});

test('the toggle cycles auto → light → dark → auto with matching icons', () => {
  assert.deepEqual([Theme.nextMode('auto'), Theme.nextMode('light'), Theme.nextMode('dark'), Theme.nextMode('x')], ['light', 'dark', 'auto', 'light']);
  assert.deepEqual(Theme.MODES.map(Theme.iconFor), ['sun-moon', 'sun', 'moon']);
});

test('applyTheme toggles the root light class and records the mode', () => {
  const classes = new Set(), root = {dataset:{}, classList:{toggle:(name, on) => on ? classes.add(name) : classes.delete(name)}};
  assert.equal(Theme.applyTheme(root, 'auto', true), 'light');
  assert.ok(classes.has('light')); assert.equal(root.dataset.themeMode, 'auto');
  assert.equal(Theme.applyTheme(root, 'dark', true), 'dark');
  assert.ok(!classes.has('light'));
});

test('vendored icons render as accessible stroke SVGs and unknown names fail loudly', () => {
  for (const name of ['sun', 'moon', 'sun-moon', 'scissors', 'play', 'pause', 'x', 'check']) assert.ok(iconNames.includes(name), name);
  const svg = icon('play', 'size-4');
  assert.match(svg, /^<svg class="icon size-4"/);
  assert.match(svg, /aria-hidden="true"/);
  assert.match(svg, /stroke="currentColor"/);
  assert.throws(() => icon('nope'), /Unknown icon/);
});
