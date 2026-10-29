/* Campaignify app shell helpers (progressive enhancement; every page works without them).
   - <time data-local> shows in the visitor's own time zone
   - cfyConfirm(text, okLabel): a themed confirm dialog (Promise<boolean>)
   - data-confirm="…" on a form or a submit button asks first
   - flash messages get a close button; success ones fade after 6 s */
(function(){
  'use strict';
  // ---- local time
  function sameDay(a, b){ return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(); }
  function fmt(d, mode){
    var now = new Date(), y = new Date(now.getTime() - 864e5);
    var date = d.toLocaleDateString([], {month: 'short', day: 'numeric', year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined});
    if(mode === 'date') return sameDay(d, now) ? 'Today' : (sameDay(d, y) ? 'Yesterday' : date);
    var t = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    if(sameDay(d, now)) return 'Today ' + t;
    if(sameDay(d, y)) return 'Yesterday ' + t;
    return date + ', ' + t;
  }
  function localTimes(root){
    [].forEach.call((root || document).querySelectorAll('time[data-local]'), function(el){
      var raw = el.getAttribute('datetime'); if(!raw) return;
      if(!/[zZ]|[+-]\d\d:?\d\d$/.test(raw)) raw += 'Z';
      var d = new Date(raw); if(isNaN(d)) return;
      el.title = d.toLocaleString(); el.textContent = fmt(d, el.dataset.local);
    });
  }
  window.cfyLocalTimes = localTimes;

  // ---- themed confirm
  var dlg = null;
  function build(){
    dlg = document.createElement('dialog'); dlg.className = 'cdlg'; dlg.setAttribute('aria-labelledby', 'cdlg-t');
    dlg.innerHTML = '<form method="dialog"><p id="cdlg-t" class="cdlg-t"></p><p class="cdlg-d small muted" hidden></p>' +
      '<div class="cdlg-go"><button class="btn dark" value="cancel" type="submit">Cancel</button><button class="btn" value="ok" type="submit" data-cdlg-ok>OK</button></div></form>';
    document.body.appendChild(dlg);
  }
  window.cfyConfirm = function(text, okLabel, detail, danger){
    if(!window.HTMLDialogElement){ return Promise.resolve(window.confirm(text)); }
    if(!dlg) build();
    dlg.querySelector('.cdlg-t').textContent = text;
    var d = dlg.querySelector('.cdlg-d'); d.hidden = !detail; d.textContent = detail || '';
    var ok = dlg.querySelector('[data-cdlg-ok]'); ok.textContent = okLabel || 'OK'; ok.classList.toggle('danger', !!danger);
    dlg.returnValue = '';
    return new Promise(function(resolve){
      dlg.addEventListener('close', function done(){ dlg.removeEventListener('close', done); resolve(dlg.returnValue === 'ok'); });
      dlg.showModal(); ok.focus();
    });
  };
  document.addEventListener('submit', function(e){
    var f = e.target, sub = e.submitter;
    var text = (sub && sub.dataset && sub.dataset.confirm) || f.dataset.confirm;
    if(!text || f._cfyOk) { f._cfyOk = false; return; }
    e.preventDefault();
    var verb = (text.match(/^(Delete|Remove|Archive|Approve|Turn off|Pause)\b/) || [])[1];
    var label = (sub && sub.dataset.confirmOk) || f.dataset.confirmOk || verb || 'Yes, continue';
    window.cfyConfirm(text, label, (sub && sub.dataset.confirmDetail) || f.dataset.confirmDetail, /delete|remove|archive/i.test(label + text)).then(function(yes){
      if(!yes) return;
      f._cfyOk = true;
      if(f.requestSubmit){ f.requestSubmit(sub || undefined); }
      else { if(sub && sub.name){ var h = document.createElement('input'); h.type = 'hidden'; h.name = sub.name; h.value = sub.value; f.appendChild(h); } f.submit(); }
    });
  }, true);

  // ---- flashes
  function flashes(){
    [].forEach.call(document.querySelectorAll('.flash-top'), function(el){
      if(el.querySelector('.flash-x')) return;
      var b = document.createElement('button'); b.type = 'button'; b.className = 'flash-x'; b.setAttribute('aria-label', 'Dismiss');
      b.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
      b.addEventListener('click', function(){ el.remove(); });
      el.appendChild(b);
      if(el.dataset.cat === 'ok'){ setTimeout(function(){ el.classList.add('fade'); setTimeout(function(){ el.remove(); }, 400); }, 6000); }
    });
  }
  function ready(){ localTimes(); flashes(); }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();
})();
