/* Presentation lifecycle only. Voice state remains owned by the existing app. */
(() => {
  const sync = () => { document.body.dataset.motionHidden = String(document.hidden); };
  document.addEventListener('visibilitychange', sync);
  window.addEventListener('pagehide', () => { document.body.dataset.motionHidden = 'true'; });
  window.addEventListener('pageshow', sync);
  sync();
})();
