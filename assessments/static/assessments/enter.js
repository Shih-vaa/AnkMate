// Progressive enhancement for the marks grid: the form works without this script.
(function () {
  var form = document.getElementById("marks-form");
  if (!form) return;
  var progress = document.getElementById("progress");

  function boxes() { return Array.prototype.slice.call(form.querySelectorAll('input[type="text"]')); }

  var typed = false;  // has anything been changed since the page loaded?

  function refresh() {
    if (!progress) return;
    var filled = 0, all = boxes();
    all.forEach(function (box) {
      var ab = form.querySelector('input[data-for="' + box.id + '"]');
      if (box.value.trim() !== "" || (ab && ab.checked)) filled += 1;
    });
    var saved = parseInt(progress.getAttribute("data-saved"), 10);
    if (!typed && filled === saved) {
      progress.textContent = saved + " of " + all.length + " saved";
    } else {
      progress.textContent = filled + " of " + all.length + " filled in \u00b7 not saved yet";
    }
  }

  // Ticking AB switches the marks box off (and clears it), unticking brings it back.
  form.addEventListener("change", function (event) {
    var ab = event.target;
    if (ab.matches && ab.matches('input[type="checkbox"][data-for]')) {
      var box = document.getElementById(ab.getAttribute("data-for"));
      box.disabled = ab.checked;
      if (ab.checked) box.value = "";
    }
    typed = true;
    refresh();
  });
  form.addEventListener("input", function () { typed = true; refresh(); });

  // Enter jumps to the next student instead of submitting the whole form.
  form.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !event.target.matches('input[type="text"]')) return;
    event.preventDefault();
    var all = boxes().filter(function (b) { return !b.disabled && !b.readOnly; });
    var next = all[all.indexOf(event.target) + 1];
    if (next) { next.focus(); next.select(); }
  });
  refresh();
})();
