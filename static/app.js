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
  // ---- video thumbnails: hover to play on a mouse, tap to play/pause on touch
  var touch = window.matchMedia && matchMedia('(hover: none)').matches;
  function players(root){
    [].forEach.call((root || document).querySelectorAll('[data-hoverplay]'), function(el){
      if(el._cfyPlay) return; el._cfyPlay = 1;
      var v = el.querySelector('video'); if(!v) return;
      if(!touch){
        el.addEventListener('mouseenter', function(){ v.play().catch(function(){}); });
        el.addEventListener('mouseleave', function(){ v.pause(); });
      }
      if(el.closest('a')) return;   // template cards are links
      el.setAttribute('role', 'button'); el.tabIndex = el.tabIndex < 0 ? 0 : el.tabIndex; el.setAttribute('aria-label', 'Play or pause');
      function toggle(e){ if(e) e.preventDefault(); if(v.paused){ v.muted = !touch ? true : false; v.play().catch(function(){ v.muted = true; v.play().catch(function(){}); }); } else v.pause(); }
      el.addEventListener('click', toggle);
      el.addEventListener('keydown', function(e){ if(e.key === ' ' || e.key === 'Enter'){ toggle(e); } });
      v.addEventListener('play', function(){ el.classList.add('playing'); });
      v.addEventListener('pause', function(){ el.classList.remove('playing'); });
    });
  }
  window.cfyPlayers = players;

  // ---- ad details dialog (cards carry a <template data-detail>)
  var jd = null;
  document.addEventListener('click', function(e){
    var b = e.target.closest('[data-detail-open]');
    if(b){ var card = b.closest('[data-job]'), t = card && card.querySelector('template[data-detail]'); jd = jd || document.querySelector('[data-jd]');
      if(!t || !jd || !jd.showModal) return; var body = jd.querySelector('[data-jd-body]'); body.innerHTML = ''; body.appendChild(t.content.cloneNode(true));
      localTimes(body); jd._from = b; jd.showModal(); return; }
    if(e.target.closest('[data-jd-close]') || (jd && e.target === jd)){ jd.close(); if(jd._from) jd._from.focus(); }
  });

  // ---- live cards: patch finished ads in place (no reload, scroll kept)
  function liveCards(){
    var box = document.querySelector('[data-live-cards]'); if(!box) return;
    var ctx = box.dataset.liveCards, url = '/app/jobs/cards';
    function pending(){ return [].slice.call(box.querySelectorAll('[data-job]')).filter(function(e){ return /pending|running/.test(e.dataset.status); }).map(function(e){ return e.dataset.job; }); }
    function total(){ return box.querySelectorAll('[data-job]').length; }
    function flash(){ var f = document.querySelector('[data-live-flash]'), n = pending().length; if(!f) return; f.hidden = !n; var t = f.querySelector('[data-live-text]'); if(t) t.textContent = 'Making ' + n + ' of ' + total() + '.'; }
    function poll(){
      var ids = pending(); if(!ids.length){ flash(); return; }
      fetch(url + '?ctx=' + ctx + '&ids=' + ids.join(','), {headers: {'Accept': 'application/json'}}).then(function(r){ return r.json(); }).then(function(d){
        Object.keys(d.cards || {}).forEach(function(id){ var c = d.cards[id], el = box.querySelector('[data-job="' + id + '"]'); if(!el || c.status === el.dataset.status) return;
          if(!c.html){ el.remove(); return; } var tmp = document.createElement('div'); tmp.innerHTML = c.html; var nw = tmp.firstElementChild; el.replaceWith(nw); players(nw); localTimes(nw); });
        flash(); if(pending().length) setTimeout(poll, 6000);
      }).catch(function(){ setTimeout(poll, 15000); });
    }
    window.cfyRefreshCard = function(id){
      return fetch(url + '?ctx=' + ctx + '&ids=' + id, {headers: {'Accept': 'application/json'}}).then(function(r){ return r.json(); }).then(function(d){
        var c = (d.cards || {})[id], el = box.querySelector('[data-job="' + id + '"]'); if(!c || !el) return null;
        if(!c.html){ el.remove(); return null; } var tmp = document.createElement('div'); tmp.innerHTML = c.html; var nw = tmp.firstElementChild; el.replaceWith(nw); players(nw); localTimes(nw); return nw; });
    };
    if(pending().length) setTimeout(poll, 6000);
  }

  // ---- approve / reject without a reload
  document.addEventListener('submit', function(e){
    var f = e.target; if(!f.matches('[data-review]') || !window.cfyRefreshCard) return;
    e.preventDefault();
    var card = f.closest('[data-job]'), id = card.dataset.job, wasFocused = card.contains(document.activeElement), key = (e.submitter || f.querySelector('button')).dataset.key;
    fetch(f.action, {method: 'POST', body: new FormData(f), headers: {'Accept': 'application/json', 'X-CSRF': f.querySelector('[name=csrf]').value}})
      .then(function(){ return window.cfyRefreshCard(id); }).then(function(nw){ if(nw && wasFocused){ var b = nw.querySelector('[data-key="' + key + '"]'); (b || nw).focus(); } });
  });
  // keyboard review: arrows move between ads, A approves, R rejects
  document.addEventListener('keydown', function(e){
    if(e.metaKey || e.ctrlKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || '')) return;
    var card = document.activeElement && document.activeElement.closest && document.activeElement.closest('[data-keyreview] [data-job]'); if(!card) return;
    var all = [].slice.call(card.parentNode.querySelectorAll('[data-job]')), i = all.indexOf(card);
    if(e.key === 'ArrowRight' || e.key === 'ArrowDown'){ e.preventDefault(); (all[i + 1] || card).focus(); }
    else if(e.key === 'ArrowLeft' || e.key === 'ArrowUp'){ e.preventDefault(); (all[i - 1] || card).focus(); }
    else if(e.key === 'a' || e.key === 'r'){ var b = card.querySelector('[data-key="' + e.key + '"]'); if(b){ e.preventDefault(); var f = b.closest('form'); if(f.requestSubmit) f.requestSubmit(b); else f.submit(); } }
  });

  // ---- Save: the share sheet on phones (falls back to a download)
  document.addEventListener('click', function(e){
    var a = e.target.closest('[data-share]'); if(!a || !touch || !navigator.share || !navigator.canShare) return;
    e.preventDefault();
    fetch(a.dataset.share).then(function(r){ return r.blob(); }).then(function(b){
      var ext = (b.type.split('/')[1] || 'bin').replace('jpeg', 'jpg'), file = new File([b], (a.dataset.shareName || 'campaignify') + '.' + ext, {type: b.type});
      if(navigator.canShare({files: [file]})) return navigator.share({files: [file]}).catch(function(){});
      location.href = a.href;
    }).catch(function(){ location.href = a.href; });
  });

  function ready(){ localTimes(); flashes(); players(); liveCards(); }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();
})();
