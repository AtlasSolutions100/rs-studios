/* The setup guide chat (web/_assistant.html). Talks to /app/assistant*. */
(function(){
  var C = window.CFY_ASST; if(!C) return;
  var LABELS = {brand:'Brand', product:'Product', photos:'Photos', claims:'Claims', audience:'Audience', goal:'Goal', first_ad:'First ad', ad_accounts:'Ad accounts', page:'Landing page'};
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function md(s){ return esc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/\n/g, '<br>'); }
  function post(url, body){ return fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-CSRF':C.csrf,'Accept':'application/json'}, body: JSON.stringify(body || {})}).then(function(r){ return r.json(); }); }

  document.querySelectorAll('[data-asst]').forEach(function(root){
    var log = root.querySelector('[data-asst-log]'), form = root.querySelector('[data-asst-form]'), ta = root.querySelector('[data-asst-text]');
    var loaded = false, busy = false, screen = root.dataset.screen || '';
    function scroll(){ log.scrollTop = log.scrollHeight; }
    function setBusy(on){ busy = on; root.classList.toggle('busy', on); var b = root.querySelector('[data-asst-send]'); if(b) b.disabled = on;
      var t = log.querySelector('.asst-typing'); if(on && !t){ t = document.createElement('div'); t.className = 'asst-msg assistant asst-typing'; t.innerHTML = '<span></span><span></span><span></span>'; log.appendChild(t); scroll(); } else if(!on && t){ t.remove(); } }
    function meta(d){
      if(d.progress){
        var done = d.progress.filter(function(p){ return p.done; }).length;
        root.querySelector('[data-asst-prog]').innerHTML = d.progress.map(function(p){ return '<li class="' + (p.done ? 'done' : (p.step === d.next ? 'now' : '')) + '">' + (p.done ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M5 12l5 5 9-10"/></svg>' : '') + esc(LABELS[p.step] || p.step) + '</li>'; }).join('');
        root.querySelector('[data-asst-step]').textContent = d.next ? 'Next: ' + (LABELS[d.next] || d.next).toLowerCase() + ' · ' + done + ' of ' + d.progress.length + ' done' : 'Set up. Ask me anything.';
      }
      if(d.free_left != null) root.querySelector('[data-asst-foot]').textContent = d.free_left > 0 ? 'Setup help is free for the next ' + d.free_left + ' credits of AI use. Anything that costs credits asks first.' : 'Replies cost about ' + d.per_reply + ' credits each (what they use). Anything else asks first.';
      var pay = root.querySelector('[data-asst-pay]'); pay.hidden = !d.need_ok; root.querySelector('[data-asst-per]').textContent = d.per_reply;
      form.hidden = !!d.need_ok;
      if(d.error) add({role:'note', text: d.error, err: true});
      if(d.balance != null) document.querySelectorAll('.pill').forEach(function(p){ p.lastChild.textContent = Number(d.balance).toLocaleString() + ' credits'; });
    }
    function card(c){
      var el = document.createElement('div'); el.className = 'acard ' + (c.status || 'open'); el.dataset.card = c.id;
      var open = c.status === 'open', h = '';
      if(c.kind === 'propose_import'){
        h = '<b>Import this product page?</b><span class="small muted acard-url">' + esc(c.url) + '</span>' + (open ? '<span class="small">Reads the page' + (c.credits ? ' with AI · about ' + c.credits + ' credits' : ' (free)') + '. Suggested claims wait for your approval.</span><div class="row"><button class="btn sm" data-act="confirm">Import' + (c.credits ? ' · ' + c.credits + ' credits' : '') + '</button><button class="btn dark sm" data-act="cancel">Not now</button></div>'
          : (c.status === 'done' ? '<span class="small ok">Imported ' + esc(c.product_name || '') + '</span>' : '<span class="small muted">' + (c.status === 'failed' ? esc(c.error || 'Failed') : 'Skipped') + '</span>'));
      } else if(c.kind === 'review_suggested_claims'){
        h = '<b>Claims found on the page</b><span class="small muted">Tick only what\'s printed on the label or you\'re allowed to say.</span>' + (open ? '<ul class="acard-claims">' + c.claims.map(function(x, i){ return '<li><label><input type="checkbox" value="' + i + '"><span>' + esc(x) + '</span></label></li>'; }).join('') + '</ul><div class="row"><button class="btn sm" data-act="claims">Approve ticked</button><button class="btn dark sm" data-act="cancel">None of these</button></div>'
          : '<span class="small ' + (c.approved && c.approved.length ? 'ok' : 'muted') + '">' + (c.approved && c.approved.length ? 'Approved: ' + esc(c.approved.join(' · ')) : 'None approved') + '</span>');
      } else if(c.kind === 'request_photo'){
        h = '<b>Add product photos</b>' + (open ? '<span class="small muted">One to three clear shots of the real product. JPG, PNG or WebP.</span><input type="file" accept="image/jpeg,image/png,image/webp" multiple data-files aria-label="Product photos"><div class="row"><button class="btn sm" data-act="upload">Upload</button><button class="btn dark sm" data-act="cancel">Later</button></div>'
          : '<span class="small ' + (c.status === 'done' ? 'ok">Photos added' : 'muted">Skipped') + '</span>');
      } else if(c.kind === 'propose_first_ad'){
        if(c.ad === 'ugc') h = '<b>A talking video of ' + esc(c.product_name) + '</b><span class="small muted">Create walks you through hooks, presenter and price (about ' + c.credits + ' credits for 10 seconds). Nothing runs until you press Generate there.</span><div class="row"><a class="btn sm" href="' + esc(c.url) + '">Open Create</a></div>';
        else h = '<b>Your first ad: ' + esc(c.template_name) + '</b><span class="small muted">An image ad of ' + esc(c.product_name) + ' from your photo.</span>' + (open ? '<span class="small">' + c.credits + ' credits · refunded if it fails</span><div class="row"><button class="btn sm" data-act="confirm">Make it · ' + c.credits + ' credits</button><button class="btn dark sm" data-act="cancel">Not now</button></div>'
          : (c.status === 'done' ? '<a class="small" href="' + esc(c.library || '#') + '">It\'s being made: see it in the library →</a>' : '<span class="small muted">Not now</span>'));
      } else if(c.kind === 'show_link'){
        h = '<a class="btn dark sm" href="' + esc(c.url) + '">' + esc(c.label || 'Open') + ' →</a>'; el.className += ' bare';
      } else if(c.kind === 'offer_choices'){
        h = '<div class="acard-chips">' + (c.choices || []).map(function(x){ return '<button type="button" class="choice" data-say="' + esc(x) + '">' + esc(x) + '</button>'; }).join('') + '</div>'; el.className += ' bare';
      }
      el.innerHTML = h; return el;
    }
    function add(it){
      var el;
      if(it.role === 'card'){ var old = log.querySelector('[data-card="' + it.card.id + '"]'); el = card(it.card); if(old){ old.replaceWith(el); return; } }
      else { el = document.createElement('div'); el.className = 'asst-msg ' + it.role + (it.err ? ' err' : ''); el.innerHTML = md(it.text); }
      log.appendChild(el); scroll();
    }
    function load(){
      if(loaded) return; loaded = true;
      fetch(C.urls.thread, {headers:{'Accept':'application/json'}}).then(function(r){ return r.json(); }).then(function(d){
        log.innerHTML = ''; (d.items || []).forEach(add); meta(d);
        if(!(d.items || []).length && d.ai_on){ setBusy(true); post(C.urls.say, {start: true, screen: screen}).then(function(r){ setBusy(false); (r.items || []).forEach(add); meta(r); }); }
      });
    }
    function say(text){
      if(busy || !text) return; add({role:'user', text: text}); setBusy(true);
      post(C.urls.say, {text: text, screen: screen}).then(function(r){ setBusy(false); (r.items || []).filter(function(i){ return i.role !== 'user'; }).forEach(add); meta(r); })
        .catch(function(){ setBusy(false); add({role:'note', text:'Something went wrong. Try again.', err: true}); });
    }
    function act(id, choice, extra){
      if(busy) return; setBusy(true);
      post(C.urls.action, Object.assign({card: id, choice: choice, screen: screen}, extra || {})).then(function(r){ setBusy(false);
        if(r.card) add({role:'card', card: r.card}); (r.items || []).forEach(add); meta(r); }).catch(function(){ setBusy(false); });
    }
    form.addEventListener('submit', function(e){ e.preventDefault(); var t = ta.value.trim(); ta.value = ''; ta.style.height = ''; say(t); });
    ta.addEventListener('keydown', function(e){ if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); form.requestSubmit(); } });
    ta.addEventListener('input', function(){ ta.style.height = ''; ta.style.height = Math.min(140, ta.scrollHeight) + 'px'; });
    root.addEventListener('click', function(e){
      var t = e.target, ch = t.closest('[data-say]'); if(ch){ say(ch.dataset.say); return; }
      var b = t.closest('[data-act]'); if(b){
        var cd = b.closest('[data-card]'), id = cd.dataset.card, a = b.dataset.act;
        if(a === 'claims'){ act(id, 'confirm', {picks: [].slice.call(cd.querySelectorAll('input:checked')).map(function(x){ return +x.value; })}); return; }
        if(a === 'upload'){ var f = cd.querySelector('[data-files]'); if(!f.files.length){ f.focus(); return; }
          var fd = new FormData(); fd.append('csrf', C.csrf); fd.append('product', C.product || ''); [].slice.call(f.files).forEach(function(x){ fd.append('photos', x); });
          setBusy(true); var cardData = (cd._card || {}); fetch(C.urls.upload + '', {method:'POST', body: fd}).then(function(){ setBusy(false); act(id, 'confirm'); }); return; }
        act(id, a); return;
      }
      if(t.closest('[data-asst-reset]')){ if(!confirm('Start the conversation over? What you set up stays.')) return; post(C.urls.reset).then(function(d){ log.innerHTML = ''; loaded = false; load(); }); return; }
      if(t.closest('[data-asst-payok]')){ post(C.urls.ok).then(function(d){ meta(d); ta.focus(); }); return; }
    });
    root._load = load;
    if(!root.hidden) load();
  });

  // The drawer: "Help me" button, "?" key, Esc.
  var drawer = document.querySelector('.asst.drawer');
  function openDrawer(){ if(!drawer) { if(C.urls.page) location.href = C.urls.page; return; } drawer.hidden = false; document.body.classList.add('asst-open'); drawer._load && drawer._load(); setTimeout(function(){ var t = drawer.querySelector('[data-asst-text]'); t && t.focus(); }, 50); }
  function closeDrawer(){ if(drawer && !drawer.hidden){ drawer.hidden = true; document.body.classList.remove('asst-open'); var b = document.querySelector('[data-help-open]'); b && b.focus(); } }
  document.addEventListener('click', function(e){ if(e.target.closest('[data-help-open]')){ e.preventDefault(); openDrawer(); } else if(e.target.closest('[data-asst-close]')) closeDrawer(); });
  document.addEventListener('keydown', function(e){
    var tag = (e.target.tagName || '').toLowerCase(); if(/input|textarea|select/.test(tag) || e.target.isContentEditable) { if(e.key === 'Escape' && drawer && drawer.contains(e.target)) closeDrawer(); return; }
    if(e.key === '?' && !e.metaKey && !e.ctrlKey){ e.preventDefault(); drawer && !drawer.hidden ? closeDrawer() : openDrawer(); }
    else if(e.key === 'Escape') closeDrawer();
  });
  if(drawer && /(^|[?&])help=1/.test(location.search)) openDrawer();
})();
