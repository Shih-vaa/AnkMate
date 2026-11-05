// Registers the service worker so the site can be installed to the home screen.
(function () {
  if (!("serviceWorker" in navigator)) return;
  var swUrl = document.currentScript && document.currentScript.dataset.sw;
  if (!swUrl) return;
  window.addEventListener("load", function () {
    navigator.serviceWorker.register(swUrl, { scope: "/" }).catch(function () {
      /* Not fatal: the site works normally without the service worker. */
    });
  });
})();
