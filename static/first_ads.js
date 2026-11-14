/* The first ads step (web/v2/onboard_first.html): counts the script's words against what the video fits, and dims a
   card whose "Make it" is unticked. The form works without this file; the server checks the same things. */
(function () {
  var form = document.querySelector('[data-first]');
  if (!form) return;
  var box = form.querySelector('[data-words]');
  var out = form.querySelector('[data-count]');
  function count() {
    if (!box || !out) return;
    var max = parseInt(box.getAttribute('data-words'), 10) || 0;
    var n = (box.value.trim().match(/\S+/g) || []).length;
    out.textContent = n + ' of about ' + max + ' words.' + (n > max ? ' Shorten it to fit the video.' : '');
    out.classList.toggle('over', n > max);
  }
  if (box) { box.addEventListener('input', count); count(); }
  // each look's preview follows its headline as it is typed (a moment after the typing stops)
  form.querySelectorAll('img[data-preview]').forEach(function (img) {
    var input = document.getElementById(img.getAttribute('data-for')), timer = 0;
    if (!input) return;
    input.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { img.src = img.getAttribute('data-preview') + '?h=' + encodeURIComponent(input.value.trim()); }, 450);
    });
  });
  form.querySelectorAll('.fa-on input').forEach(function (tick) {
    function show() { tick.closest('.fa-card').classList.toggle('off', !tick.checked); }
    tick.addEventListener('change', show);
    show();
  });
})();
