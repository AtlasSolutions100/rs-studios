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

  // ---- confirm your email (window.CFY_VERIFY is set only while it's needed, web/base.html)
  // cfyVerify(reason) opens a small box: we email a 6-digit code (unless the last one still
  // works), they type it, and it resolves true once the email is confirmed. Forms marked
  // data-needs-verify="video|cap" open it before submitting, then submit as they were.
  var vdlg = null;
  function vpost(url, body){
    var V = window.CFY_VERIFY;
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF': V.csrf},
      body: JSON.stringify(body || {})}).then(function(r){ return r.json().catch(function(){ return {error: 'Something went wrong. Try again.'}; }); });
  }
  function vbuild(){
    vdlg = document.createElement('dialog'); vdlg.className = 'cdlg vdlg'; vdlg.setAttribute('aria-labelledby', 'vdlg-t');
    vdlg.innerHTML = '<form data-vform novalidate><p id="vdlg-t" class="cdlg-t"></p><p class="small muted" data-vmsg style="margin:0 0 14px"></p>' +
      '<label for="vdlg-code" class="sr">6-digit code</label><input id="vdlg-code" class="vcode" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]*" maxlength="6" placeholder="123456">' +
      '<p class="small" data-verr role="alert" style="margin:8px 0 0" hidden></p>' +
      '<p class="small muted" style="margin:12px 0 0">No email? Check spam, or <button type="button" class="linkbtn" data-vresend>Resend code</button></p>' +
      '<div class="cdlg-go"><button class="btn dark" type="button" data-vcancel>Not now</button><button class="btn" type="submit" data-vok>Confirm</button></div></form>';
    document.body.appendChild(vdlg);
  }
  window.cfyVerify = function(reason){
    var V = window.CFY_VERIFY;
    if(!V) return Promise.resolve(true);
    if(!window.HTMLDialogElement || !window.fetch){ return Promise.resolve(false); }
    if(!vdlg) vbuild();
    if(vdlg.open) return Promise.resolve(false);
    var form = vdlg.querySelector('[data-vform]'), inp = vdlg.querySelector('.vcode'), err = vdlg.querySelector('[data-verr]'),
        msg = vdlg.querySelector('[data-vmsg]'), ok = vdlg.querySelector('[data-vok]'), again = vdlg.querySelector('[data-vresend]');
    vdlg.querySelector('.cdlg-t').textContent = reason === 'cap' ? 'Confirm your email to keep making ads' : 'Confirm your email to make video ads';
    msg.textContent = 'We\'re emailing a 6-digit code to ' + V.email + '.';
    inp.value = ''; err.hidden = true; ok.disabled = false; ok.textContent = 'Confirm';
    function say(t, bad){ err.hidden = !t; err.textContent = t || ''; err.classList.toggle('warnc', !!bad); }
    vpost(V.send, {auto: true}).then(function(r){
      if(r.verified){ finish(true); return; }
      if(r.message) msg.textContent = r.message;
      if(r.error) say(r.error, true);
    }).catch(function(){});
    var done = false, resolveFn;
    function finish(yes){
      if(done) return; done = true;
      if(yes){ window.CFY_VERIFY = null; var bn = document.querySelector('[data-verify-banner]'); if(bn) bn.remove(); }
      if(vdlg.open) vdlg.close();
      resolveFn(yes);
    }
    form.onsubmit = function(e){
      e.preventDefault(); var code = inp.value.replace(/\D/g, '');
      if(code.length !== 6){ say('Enter the 6-digit code from the email.', true); inp.focus(); return; }
      ok.disabled = true; ok.textContent = 'Checking…';
      vpost(V.check, {code: code}).then(function(r){
        ok.disabled = false; ok.textContent = 'Confirm';
        if(r.ok){ finish(true); return; }
        say(r.error || 'That didn\'t work. Try again.', true); inp.select();
      }).catch(function(){ ok.disabled = false; ok.textContent = 'Confirm'; say('Something went wrong. Try again.', true); });
    };
    again.onclick = function(){
      again.disabled = true;
      vpost(V.send, {}).then(function(r){ again.disabled = false; if(r.verified){ finish(true); return; }
        if(r.ok){ msg.textContent = r.message; say(''); inp.value = ''; inp.focus(); } else say(r.error, true); })
        .catch(function(){ again.disabled = false; });
    };
    vdlg.querySelector('[data-vcancel]').onclick = function(){ finish(false); };
    vdlg.onclose = function(){ finish(false); };
    return new Promise(function(resolve){ resolveFn = resolve; vdlg.showModal(); inp.focus(); });
  };
  document.addEventListener('click', function(e){
    if(!e.target.closest('[data-verify-open]')) return;
    window.cfyVerify('video').then(function(yes){ if(yes) vdone('Email confirmed. Video ads are unlocked.'); });
  });
  function vdone(text){
    var m = document.querySelector('main') || document.body, d = document.createElement('div');
    d.className = 'flash flash-top'; d.dataset.cat = 'ok'; d.setAttribute('role', 'status'); d.innerHTML = '<span></span>';
    d.firstChild.textContent = text; m.insertBefore(d, m.firstChild); flashes();
  }
  // Runs before the data-confirm hook below, so a video form asks for the code first and its usual question after.
  document.addEventListener('submit', function(e){
    var f = e.target, need = f.dataset && f.dataset.needsVerify;
    if(!need || !window.CFY_VERIFY || !window.HTMLDialogElement) return;
    e.preventDefault(); e.stopImmediatePropagation();
    var sub = e.submitter;
    window.cfyVerify(need).then(function(yes){
      if(!yes) return;
      if(f.requestSubmit){ f.requestSubmit(sub || undefined); } else f.submit();
    });
  }, true);
  // A video that was refused on the server (no script at the time) opens the box on the next page.
  function vauto(){ var V = window.CFY_VERIFY; if(V && V.auto && !V.inline) window.cfyVerify(V.auto).then(function(yes){ if(yes) vdone('Email confirmed. Press the button again to start.'); }); }

  // ---- first-party funnel events from the app (site_events.py CLIENT_EVENTS; the server drops anything else)
  window.cfyEvent = function(name, detail){
    try {
      var b = JSON.stringify({name: name, path: location.pathname, detail: detail || null});
      if(navigator.sendBeacon && navigator.sendBeacon('/e', new Blob([b], {type: 'text/plain'}))) return;
      fetch('/e', {method: 'POST', headers: {'Content-Type': 'text/plain'}, body: b, keepalive: true, credentials: 'same-origin'}).catch(function(){});
    } catch(e) {}
  };

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
    // share links: copy the link next to the button
    var cp = e.target.closest('[data-copy]');
    if(cp){ var inp = document.getElementById(cp.dataset.copy); if(!inp) return; inp.select();
      var done = function(){ cp.textContent = 'Copied'; };
      if(navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(inp.value).then(done, function(){ try { document.execCommand('copy'); done(); } catch(_){} });
      else { try { document.execCommand('copy'); done(); } catch(_){} } return; }
    var sa = e.target.closest('[data-select-all]'); if(sa) sa.select();
  });

  // ---- live cards: patch finished ads in place (no reload, scroll kept)
  function liveCards(){
    var box = document.querySelector('[data-live-cards]'); if(!box) return;
    var ctx = box.dataset.liveCards, url = '/app/jobs/cards';
    function pending(){ return [].slice.call(box.querySelectorAll('[data-job]')).filter(function(e){ return /pending|running/.test(e.dataset.status); }).map(function(e){ return e.dataset.job; }); }
    function total(){ return box.querySelectorAll('[data-job]').length; }
    function flash(){ var f = document.querySelector('[data-live-flash]'), n = pending().length; if(!f) return; f.hidden = !n; var t = f.querySelector('[data-live-text]'); if(t) t.textContent = 'Making ' + n + ' of ' + total() + '.'; var dn = document.querySelector('[data-live-done]'); if(dn && !n) dn.hidden = false; }
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

  function ready(){ localTimes(); flashes(); players(); liveCards(); [].forEach.call(document.querySelectorAll('[data-verify-open]'), function(b){ b.hidden = !window.CFY_VERIFY; }); vauto(); }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready); else ready();
})();
