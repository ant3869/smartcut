// Theme preference, OpenEval-style: `:root.light` switches every token. "auto" follows the OS
// and keeps following it live; "light"/"dark" pin the choice. index.html repeats resolve()
// inline so the first paint never flashes the wrong theme.
export const THEME_KEY = 'anna-cutroom-theme';
export const MODES = ['auto', 'light', 'dark'];
export const normalize = mode => MODES.includes(mode) ? mode : 'auto';
export const resolve = (mode, prefersLight) => normalize(mode) === 'auto' ? (prefersLight ? 'light' : 'dark') : normalize(mode);
export const nextMode = mode => MODES[(MODES.indexOf(normalize(mode)) + 1) % MODES.length];
export const label = mode => ({auto:'Auto (system)', light:'Light', dark:'Dark'})[normalize(mode)];
export const iconFor = mode => ({auto:'sun-moon', light:'sun', dark:'moon'})[normalize(mode)];

export function applyTheme(root, mode, prefersLight) {
  const theme = resolve(mode, prefersLight);
  root.classList.toggle('light', theme === 'light');
  root.dataset.themeMode = normalize(mode);
  return theme;
}
