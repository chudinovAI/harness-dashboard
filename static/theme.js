(() => {
  const storageKey = 'harness-dashboard-theme';
  const systemTheme = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  let preference = 'system';

  try {
    const stored = localStorage.getItem(storageKey);
    if (stored === 'light' || stored === 'dark') preference = stored;
  } catch (_) {
    // The selector still works for this page when storage is unavailable.
  }

  function applyTheme() {
    const dark = preference === 'dark' || (preference === 'system' && systemTheme && systemTheme.matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  }

  applyTheme();

  if (systemTheme) {
    if (systemTheme.addEventListener) systemTheme.addEventListener('change', applyTheme);
    else if (systemTheme.addListener) systemTheme.addListener(applyTheme);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const selector = document.getElementById('theme-select');
    selector.value = preference;
    selector.addEventListener('change', () => {
      preference = selector.value;
      try {
        if (preference === 'system') localStorage.removeItem(storageKey);
        else localStorage.setItem(storageKey, preference);
      } catch (_) {
        // Keep the selection active for this page even if it cannot be saved.
      }
      applyTheme();
    });
  });
})();
