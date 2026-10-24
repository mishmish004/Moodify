// The "hindi mai" / "in english" pill. Each button flips data-lang on its nearest
// [data-lang-scope]; CSS (.swap-grid in global.css) cross-fades the two copies.

export function setLang(scope: Element, hi: boolean) {
  scope.setAttribute('data-lang', hi ? 'hi' : 'en');
  scope.querySelectorAll('.swap-grid > [lang]').forEach((el) => {
    el.setAttribute('aria-hidden', String((el.getAttribute('lang') === 'hi') !== hi));
  });
  scope.querySelectorAll<HTMLButtonElement>('[data-lang-toggle]').forEach((btn) => {
    btn.setAttribute('aria-pressed', String(hi));
    btn.textContent = hi ? 'in english' : 'hindi mai';
  });
}

export function initLangToggles(root: ParentNode = document) {
  root.querySelectorAll<HTMLButtonElement>('[data-lang-toggle]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const scope = btn.closest('[data-lang-scope]');
      if (scope) setLang(scope, scope.getAttribute('data-lang') !== 'hi');
    });
  });
}
