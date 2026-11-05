// Progressive enhancement for the attendance form: the page works without this script.
(function () {
  document.querySelectorAll("[data-mark-all]").forEach(function (button) {
    button.addEventListener("click", function () {
      var code = button.getAttribute("data-mark-all");
      document.querySelectorAll('#mark-form input[type="radio"][value="' + code + '"]').forEach(function (radio) {
        if (!radio.disabled) radio.checked = true;
      });
      updateTally();
    });
  });

  var tally = document.getElementById("tally");
  function updateTally() {
    if (!tally) return;
    var counts = {};
    document.querySelectorAll('#mark-form input[type="radio"]:checked').forEach(function (radio) {
      counts[radio.value] = (counts[radio.value] || 0) + 1;
    });
    var present = (counts.P || 0) + (counts.L || 0) + (counts.H || 0);
    var away = (counts.A || 0) + (counts.E || 0);
    tally.textContent = present + " present, " + away + " absent or on leave";
  }
  document.addEventListener("change", function (event) {
    if (event.target && event.target.matches('#mark-form input[type="radio"]')) updateTally();
  });
  updateTally();
})();
