/* The campaign desk (desk.py; web/v2/desk.html is its home, web/v2/desk_thread.html one campaign's conversation).
   The sentence box -> /app/desk/plan, which starts a campaign or adds a turn to the one open, then the page of that
   campaign is shown. There, the newest plan is open: a tick, an edit, a part added or removed -> /app/desk/quote
   (no model, just the prices again, and the rows are kept on the turn). Make it -> the claims check Studio runs,
   then /app/desk/run with the total the button showed; the server starts nothing when that total is not the one it
   works out. Ads still being made are refreshed in place until they are done. */
(function(){
  var B = window.CFY_DESK, root = document.querySelector('[data-desk]');
  if(!B || !root) return;
  function $(s, el){ return (el || root).querySelector(s); }
  function $$(s, el){ return Array.prototype.slice.call((el || root).querySelectorAll(s)); }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function credits(n){ return Number(n).toLocaleString('en-US') + (Number(n) === 1 ? ' credit' : ' credits'); }
  function post(url, body){
    return fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF': B.csrf, 'X-Requested-With': 'fetch'}, body: JSON.stringify(body)})
      .then(function(r){ return r.json().catch(function(){ return {error: 'Something went wrong. Try again.'}; }).then(function(d){ d._status = r.status; return d; }); });
  }
  function opt(v, label, cur){ return '<option value="' + esc(v) + '"' + (String(cur) === String(v) ? ' selected' : '') + '>' + esc(label) + '</option>'; }
  function go(url){                      // the same page with a new place on it still has to be loaded again
    var to = new URL(url, location.href);
    if(to.pathname === location.pathname){ location.hash = to.hash; location.reload(); } else location.href = url;
  }

  // ---------------------------------------------------------------- the sentence box (both pages)
  var ask = $('[data-ask]'), form = $('[data-ask-form]'), sayEl = $('[data-say]'), planBtn = $('[data-plan]');
  var busy = false;
  function say(msg){ if(!sayEl) return; sayEl.hidden = !msg; sayEl.textContent = msg || ''; }
  function plan(){
    var text = ask.value.trim().replace(/\s+/g, ' ');
    if(text.length < 8){ say(B.thread ? 'Say what to add or change, in a sentence.' : 'Say what you want to launch, in a sentence.'); ask.focus(); return; }
    if(busy) return;
    busy = true; say('');
    if(!B.thread && B.urls.clarify && clar){ ask_first(text); return; }
    send(text, []);
  }
  // Planning: the request goes to /app/desk/plan with any answers; the steps show while it works.
  function send(text, answers){
    busy = true; planBtn.disabled = true;
    var label = planBtn.querySelector('span') ? planBtn.querySelector('span').textContent : planBtn.textContent;
    setLabel('Planning');
    var stop = working(['Reading your brand and product', 'Choosing the parts of the campaign', 'Pricing each part']);
    function back(msg){ busy = false; planBtn.disabled = false; setLabel(label); stop(false); show_form(); say(msg); }
    post(B.urls.plan, {ask: text, product: B.product, thread: B.thread || null, answers: answers}).then(function(r){
      if(!r.url){ back(r.error || 'That didn\'t come back. Try again.'); return; }
      stop(true); go(r.url);
    }).catch(function(){ back('That didn\'t come back. Try again.'); });
  }
  function setLabel(t){ var sp = planBtn.querySelector('span'); if(sp) sp.textContent = t; else planBtn.textContent = t; }

  // ---------------------------------------------------------------- the questions before a new campaign
  var clar = $('[data-clarify]'), work = $('[data-working]');
  function show_form(){ root.classList.remove('dk-asking'); if(form) form.hidden = false; if(clar){ clar.hidden = true; clar.innerHTML = ''; } }
  // A list of what is happening, one line at a time; the last line waits for the answer.
  function working(lines){
    if(!work) return function(){};
    work.hidden = false;
    work.innerHTML = '<p class="dk-work-h">Working on it</p><ol>' + lines.map(function(l){ return '<li><span class="spinner sm"></span>' + esc(l) + '</li>'; }).join('') + '</ol>';
    var items = $$('li', work), i = 0, t;
    function tick(){ items.forEach(function(li, j){ li.className = j < i ? 'done' : (j === i ? 'now' : ''); }); }
    tick();
    t = setInterval(function(){ if(i < items.length - 1){ i++; tick(); } }, 1600);
    return function(ok){ clearInterval(t); if(ok){ i = items.length; tick(); } else { work.hidden = true; work.innerHTML = ''; } };
  }
  function ask_first(text){
    root.classList.add('dk-asking');
    if(form) form.hidden = true;
    clar.hidden = false;
    clar.innerHTML = '<p class="dk-said">' + esc(text) + '</p><p class="dk-think" role="status"><span class="spinner sm"></span>Reading your request</p>';
    post(B.urls.clarify, {ask: text, product: B.product}).then(function(r){
      if(r.error){ busy = false; show_form(); say(r.error); return; }
      if(!(r.questions || []).length){ send(text, []); return; }
      cards(text, r.questions);
    }).catch(function(){ send(text, []); });
  }
  function cards(text, qs){
    var picks = qs.map(function(){ return null; }), at = 0;
    function rec(q){ var o = q.options.filter(function(o){ return o.recommended; })[0] || q.options[0]; return o.label; }
    function finish(){
      var answers = qs.map(function(q, i){ return picks[i] ? {q: q.q, a: picks[i]} : null; }).filter(Boolean);
      clar.innerHTML = '<p class="dk-said">' + esc(text) + '</p>' + (answers.length ? '<dl class="dk-qa">' + answers.map(function(a){ return '<dt>' + esc(a.q) + '</dt><dd>' + esc(a.a) + '</dd>'; }).join('') + '</dl>' : '');
      send(text, answers);
    }
    function draw(){
      var q = qs[at], last = at === qs.length - 1, id = 'dk-cq-' + at;
      clar.innerHTML = '<p class="dk-said">' + esc(text) + '</p>'
        + '<section class="dk-card" aria-labelledby="' + id + '">'
        + '<div class="dk-card-top"><span>A few quick questions so the plan fits</span>'
        + '<span class="dk-card-nav"><button type="button" class="dk-card-arrow" data-prev aria-label="Previous question"' + (at ? '' : ' disabled') + '>&lsaquo;</button>'
        + '<span aria-live="polite">' + (at + 1) + ' of ' + qs.length + '</span>'
        + '<button type="button" class="dk-card-arrow" data-next aria-label="Next question"' + (picks[at] && !last ? '' : ' disabled') + '>&rsaquo;</button></span></div>'
        + '<h3 class="dk-card-q" id="' + id + '">' + esc(q.q) + '</h3>'
        + '<div class="dk-opts" role="radiogroup" aria-labelledby="' + id + '">' + q.options.map(function(o, i){
            var on = picks[at] === o.label;
            return '<button type="button" class="dk-opt2" role="radio" aria-checked="' + on + '" data-o="' + i + '"><b>' + esc(o.label) + '</b>'
              + (o.hint ? '<span>' + esc(o.hint) + '</span>' : '') + (o.recommended ? '<em>Recommended</em>' : '') + '</button>';
          }).join('') + '</div>'
        + '<label class="dk-own"><span class="sr">Or type your own answer</span><input type="text" maxlength="80" placeholder="Or type your own answer" data-own value="'
        + esc(picks[at] && !q.options.some(function(o){ return o.label === picks[at]; }) ? picks[at] : '') + '"></label>'
        + '<div class="dk-card-foot"><button type="button" class="linkbtn" data-skip>Skip, just plan it</button>'
        + '<span><button type="button" class="btn sm ghost" data-recs>Use the recommended answers</button>'
        + '<button type="button" class="btn sm" data-go' + (picks[at] ? '' : ' disabled') + '>' + (last ? 'Plan it' : 'Next') + '</button></span></div>'
        + '</section>';
      $$('[data-o]', clar).forEach(function(b){
        b.addEventListener('click', function(){
          picks[at] = q.options[+b.dataset.o].label;
          if(last){ draw(); $('[data-go]', clar).focus(); } else { at++; draw(); focusFirst(); }
        });
      });
      var own = $('[data-own]', clar);
      own.addEventListener('input', function(){ var v = own.value.trim(); $('[data-go]', clar).disabled = !v && !picks[at]; });
      own.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ e.preventDefault(); $('[data-go]', clar).click(); } });
      $('[data-go]', clar).addEventListener('click', function(){
        var v = own.value.trim(); if(v) picks[at] = v;
        if(!picks[at]) return;
        if(last) finish(); else { at++; draw(); focusFirst(); }
      });
      $('[data-prev]', clar).addEventListener('click', function(){ if(at){ at--; draw(); focusFirst(); } });
      $('[data-next]', clar).addEventListener('click', function(){ if(at < qs.length - 1){ at++; draw(); focusFirst(); } });
      $('[data-skip]', clar).addEventListener('click', function(){ picks = picks.map(function(){ return null; }); finish(); });
      $('[data-recs]', clar).addEventListener('click', function(){ picks = qs.map(function(q, i){ return picks[i] || rec(q); }); finish(); });
    }
    function focusFirst(){ var b = $('[aria-checked="true"]', clar) || $('[data-o]', clar); if(b) b.focus(); }
    draw(); focusFirst();
  }
  if(form && ask && B.product){
    form.addEventListener('submit', function(e){ e.preventDefault(); plan(); });
    ask.addEventListener('keydown', function(e){ if(e.key === 'Enter' && (e.metaKey || e.ctrlKey)){ e.preventDefault(); plan(); } });
  }
  // An example card puts its sentence in the box; its clip plays while the card is pointed at.
  var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  // Ready-made jobs: a card puts its sentence in the box and starts the agent (its questions first, on a new campaign)
  $$('[data-job-ask]').forEach(function(b){
    b.addEventListener('click', function(){ if(!ask || busy) return; ask.value = b.dataset.jobAsk; plan(); });
  });
  var jrow = $('[data-jobs-row]');
  $$('[data-jobs-go]').forEach(function(b){
    b.addEventListener('click', function(){ if(jrow) jrow.scrollBy({left: +b.dataset.jobsGo * jrow.clientWidth * 0.8, behavior: 'smooth'}); });
  });
  $$('[data-try]').forEach(function(b){
    b.addEventListener('click', function(){ if(!ask) return; ask.value = b.dataset.try; ask.focus(); ask.setSelectionRange(ask.value.length, ask.value.length); });
  });
  $$('.dk-ex').forEach(function(b){
    var v = b.querySelector('video'); if(!v || still) return;
    function on(){ b.classList.add('playing'); var p = v.play(); if(p && p.catch) p.catch(function(){ b.classList.remove('playing'); }); }
    function off(){ b.classList.remove('playing'); v.pause(); }
    b.addEventListener('mouseenter', on); b.addEventListener('mouseleave', off); b.addEventListener('focus', on); b.addEventListener('blur', off);
  });
  // The kinds of example: one row shows at a time.
  $$('[data-cat-pick]').forEach(function(chip){
    chip.addEventListener('click', function(){
      $$('[data-cat-pick]').forEach(function(c){ c.setAttribute('aria-pressed', c === chip ? 'true' : 'false'); });
      $$('[data-cat]').forEach(function(row){ row.hidden = row.dataset.cat !== chip.dataset.catPick; });
    });
  });

  // ---------------------------------------------------------------- ads still being made: refresh them in place
  function watch(){
    if(!$('[data-tiles][data-busy]')) return;
    setTimeout(function(){
      fetch(location.pathname, {credentials: 'same-origin', headers: {'X-Requested-With': 'fetch'}}).then(function(r){ return r.text(); }).then(function(html){
        var doc = new DOMParser().parseFromString(html, 'text/html');
        $$('[data-tiles]').forEach(function(el){
          var now = doc.querySelector('[data-tiles="' + el.dataset.tiles + '"]'); if(!now) return;
          var card = el.closest('.dk-done'), fresh = now.closest('.dk-done');
          if(card && fresh) card.innerHTML = fresh.innerHTML;
        });
        watch();
      }).catch(function(){ watch(); });
    }, 8000);
  }
  watch();

  // ---------------------------------------------------------------- the open plan
  var wrap = $('[data-plan-wrap]');
  if(!wrap || !B.plan) return;
  var MAKES = {image: 1, presenter: 1, voiceover: 1, product_video: 1};
  var STAGES = {make: 'Make', write: 'Write', capture: 'Capture', followup: 'Follow up', launch: 'Launch'};
  var WORDS = {image: 'Headline', presenter: 'What the presenter says', voiceover: 'What the narrator says', product_video: 'The shot'};
  var S = {ask: B.plan.ask || '', parts: B.plan.parts, q: B.plan, turn: B.plan.turn, editing: '', adding: false, busy: false, seq: 0, n: 0};
  var rowsEl = $('[data-rows]'), runBtn = $('[data-run]'), errEl = $('[data-err]');
  function err(html){ errEl.hidden = !html; errEl.innerHTML = html || ''; }
  function part(id){ for(var i = 0; i < S.parts.length; i++) if(S.parts[i].id === id) return S.parts[i]; return null; }
  function who(id){ for(var i = 0; i < B.presenters.length; i++) if(B.presenters[i].id === id) return B.presenters[i]; return B.presenters[0] || null; }

  function editor(p){
    var h = '';
    if(MAKES[p.kind]){
      h += '<label>' + WORDS[p.kind] + '<textarea rows="' + (p.kind === 'image' ? 2 : 3) + '" maxlength="900" data-f="text">' + esc(p.text) + '</textarea></label>';
      if(p.kind === 'image'){
        var c = ''; for(var i = 1; i <= B.max_count; i++) c += opt(i, i === 1 ? '1 version' : i + ' versions', p.count || 1);
        h += '<label>How many<select data-f="count">' + c + '</select></label>';
      }
      if(p.kind === 'presenter'){
        var w = who(p.presenter);
        h += '<label>Presenter<select data-f="presenter">' + B.presenters.map(function(x){ return opt(x.id, x.name, w && w.id); }).join('') + '</select></label>';
        if(w && w.places.length) h += '<label>Where<select data-f="situation">' + opt('', 'Their usual place', p.situation || '') + w.places.map(function(x){ return opt(x.id, x.name, p.situation || ''); }).join('') + '</select></label>';
      }
    } else if(p.kind === 'copy'){
      h += '<label>Platform<select data-f="platform">' + B.platforms.map(function(x){ return opt(x.id, x.name, p.platform); }).join('') + '</select></label>';
      h += '<label>How many<select data-f="n">' + opt(3, '3 versions', p.n) + opt(5, '5 versions', p.n) + '</select></label>';
    } else if(p.kind === 'page'){
      h += '<label>Page title<input type="text" maxlength="120" data-f="title" value="' + esc(p.title || '') + '"></label>';
    } else if(p.kind === 'flow'){
      h += '<label>Follow-up<select data-f="template">' + B.flows.map(function(x){ return opt(x.id, x.name, p.template); }).join('') + '</select></label>';
    } else if(p.kind === 'story'){
      h += '<label>The story idea<textarea rows="3" maxlength="300" data-f="text" placeholder="Bob makes bad ads. Then he finds us.">' + esc(p.text || '') + '</textarea></label>';
      h += '<label>Look<select data-f="style">' + opt('photo', 'Real photos, funny', p.style) + opt('animated', 'Animated, 3D cartoon', p.style) + '</select></label>';
      var sc = ''; for(var k = 3; k <= 6; k++) sc += opt(k, k + ' scenes', p.scenes || (p.style === 'animated' ? 3 : 5));
      h += '<label>How long<select data-f="scenes">' + sc + '</select></label>';
    }
    return '<div class="dk-edit" data-edit>' + (h ? '<div class="dk-fields">' + h + '</div>' : '') +
      '<div class="dk-edit-act"><button type="button" class="btn dark sm" data-done>Done</button>' +
      '<button type="button" class="linkbtn" data-remove>Remove this part</button></div></div>';
  }
  function row(p, first){
    var open = S.editing === p.id, can = !p.locked && !p.error && !((MAKES[p.kind] || p.kind === 'story') && !p.text);
    var label = p.locked ? '<a href="' + esc(B.urls.billing) + '#plans">' + esc(p.label) + '</a>' : esc(p.label);
    return '<li class="dk-row' + (p.on ? '' : ' off') + '" data-id="' + esc(p.id) + '">' +
      '<span class="dk-stage">' + (first ? esc(STAGES[p.stage]) : '') + '</span>' +
      '<button type="button" class="dk-tick" data-tick aria-pressed="' + (p.on ? 'true' : 'false') + '"' + (can ? '' : ' disabled') +
        ' aria-label="' + esc((p.on ? 'Included: ' : 'Left out: ') + p.name) + '"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></button>' +
      '<div class="dk-body"><b>' + esc(p.name) + '</b>' +
        (open ? editor(p) : (p.words ? '<span class="dk-words">' + esc(p.words) + '</span>' : '')) +
        (p.error ? '<span class="dk-row-err">' + esc(p.error) + '</span>' : '') + '</div>' +
      '<div class="dk-price"><span>' + label + '</span>' +
        '<button type="button" class="btn dark sm" data-edit-open aria-expanded="' + (open ? 'true' : 'false') + '">Edit</button></div></li>';
  }
  function render(){
    var seen = {}, held = document.activeElement && rowsEl.contains(document.activeElement) ? document.activeElement : null;
    var key = held ? [held.closest('[data-id]').dataset.id, held.dataset.f != null ? '[data-f="' + held.dataset.f + '"]' : held.hasAttribute('data-tick') ? '[data-tick]' : '[data-edit-open]'] : null;
    rowsEl.innerHTML = S.parts.map(function(p){ var first = !seen[p.stage]; seen[p.stage] = 1; return row(p, first); }).join('');
    if(key){ var li = rowsEl.querySelector('[data-id="' + key[0] + '"]'), el = li && (li.querySelector(key[1]) || li.querySelector('[data-edit-open]')); if(el && !el.disabled) el.focus(); }
    bar(); menu();
  }
  function bar(){
    var q = S.q || {total: 0, count: 0, balance: B.balance, short: 0, copy: 0};
    $('[data-total]').textContent = !q.count ? 'Nothing ticked' : q.total ? (q.copy ? 'Up to ' : '') + credits(q.total) : 'No credits';
    var sum = $('[data-sum]');
    if(q.short && q.upgrade) sum.innerHTML = 'You have ' + esc(credits(q.balance)) + ', so you need ' + esc(credits(q.short)) + ' more. <button type="button" class="linkbtn" data-upgrade-open>Upgrade your plan</button>';
    else if(q.short) sum.innerHTML = 'You have ' + esc(credits(q.balance)) + ', so you need ' + esc(credits(q.short)) + ' more. ' + (q.pack ? 'The ' + esc(q.pack.name) + ' pack ($' + esc(q.pack.usd) + ') covers it. ' : '') + '<a href="' + esc(B.urls.billing) + '">Get credits</a>';
    else sum.textContent = (q.count === 1 ? '1 part ticked' : q.count + ' parts ticked') + '. You have ' + credits(q.balance) + '.';
    var many = $('[data-many-wrap]'); many.hidden = !q.confirm;
    if(q.confirm) $('[data-many-text]').textContent = 'Yes, make all ' + q.videos + ' videos.'; else $('[data-many]').checked = false;
    runBtn.textContent = !q.count ? 'Nothing ticked' : q.total ? 'Make it · ' + credits(q.total) : 'Set it up';
    runBtn.disabled = S.busy || !q.count || (!!q.short && !q.upgrade) || (!!q.confirm && !$('[data-many]').checked);
    runBtn.dataset.total = q.total;
  }
  function menu(){
    var m = $('[data-add-menu]'), b = $('[data-add-open]');
    b.setAttribute('aria-expanded', S.adding ? 'true' : 'false'); m.hidden = !S.adding;
    if(!S.adding) return;
    var makes = S.parts.filter(function(p){ return MAKES[p.kind]; }).length, have = {};
    S.parts.forEach(function(p){ have[p.kind] = 1; });
    var items = B.add.filter(function(a){ return MAKES[a.kind] ? makes < B.max_makes : !have[a.kind]; });
    m.innerHTML = items.length ? '<span>It joins the plan with its own price.</span><div>' + items.map(function(a){
      return a.locked ? '<a class="dk-opt" href="' + esc(B.urls.billing) + '#plans">' + esc(a.name) + '<em>' + esc(a.locked) + ' plan</em></a>'
        : '<button type="button" class="dk-opt" data-add="' + esc(a.kind) + '">' + esc(a.name) + '</button>'; }).join('') + '</div>'
      : '<span>Everything a campaign can have is already in the plan.</span>';
  }

  // pricing again, with no model; the rows are kept on the plan's turn so a reload shows them
  function requote(){
    var n = ++S.seq;
    return post(B.urls.quote, {product: B.product, parts: S.parts, turn: S.turn}).then(function(r){
      if(n !== S.seq) return;
      if(r.error || !r.parts){ err(esc(r.error || 'The prices didn\'t come back. Try again.')); return; }
      var open = S.editing && rowsEl.querySelector('[data-id="' + S.editing + '"]'), vals = {};
      if(open) $$('[data-f]', open).forEach(function(el){ vals[el.dataset.f] = el.value; });   // what is being typed survives the redraw
      S.q = r; S.parts = r.parts; err(''); render();
      var now = open && rowsEl.querySelector('[data-id="' + S.editing + '"]');
      if(now) $$('[data-f]', now).forEach(function(el){
        var v = vals[el.dataset.f]; if(v == null) return;
        if(el.tagName === 'SELECT' && !Array.prototype.some.call(el.options, function(o){ return o.value === v; })) return;
        el.value = v;
      });
    }).catch(function(){ if(n === S.seq) err('The prices didn\'t come back. Try again.'); });
  }
  function readEdit(li, p){
    $$('[data-f]', li).forEach(function(el){
      var f = el.dataset.f, v = el.value;
      if(f === 'text' || f === 'title') v = v.trim().replace(/\s+/g, ' ');
      if(f === 'count' || f === 'n' || f === 'scenes') v = +v;
      if(f === 'presenter' && v !== p.presenter) p.situation = '';
      p[f] = v;
    });
    if((MAKES[p.kind] || p.kind === 'story') && p.text && !p.error) p.on = true;
  }
  rowsEl.addEventListener('click', function(e){
    var li = e.target.closest('[data-id]'); if(!li) return;
    var p = part(li.dataset.id); if(!p) return;
    if(e.target.closest('[data-tick]')){ p.on = !p.on; render(); requote(); }
    else if(e.target.closest('[data-edit-open]')){
      var was = S.editing; if(was){ var o = part(was), ol = rowsEl.querySelector('[data-id="' + was + '"]'); if(o && ol) readEdit(ol, o); }
      S.editing = was === p.id ? '' : p.id; render(); if(was) requote();
      var t = rowsEl.querySelector('[data-id="' + p.id + '"] [data-f]'); if(S.editing && t) t.focus();
    }
    else if(e.target.closest('[data-done]')){ readEdit(li, p); S.editing = ''; render(); requote(); var eb = rowsEl.querySelector('[data-id="' + p.id + '"] [data-edit-open]'); if(eb) eb.focus(); }
    else if(e.target.closest('[data-remove]')){ S.parts = S.parts.filter(function(x){ return x !== p; }); S.editing = ''; render(); requote(); $('[data-add-open]').focus(); }
  });
  rowsEl.addEventListener('change', function(e){           // the presenter decides which places can be picked
    if(e.target.dataset.f !== 'presenter') return;
    var li = e.target.closest('[data-id]'), p = part(li.dataset.id); readEdit(li, p); render();
  });
  $('[data-add-open]').addEventListener('click', function(){ S.adding = !S.adding; menu(); });
  $('[data-add-menu]').addEventListener('click', function(e){
    var b = e.target.closest('[data-add]'); if(!b) return;
    var kind = b.dataset.add, p = {id: 'a' + (++S.n) + Date.now().toString(36).slice(-4), kind: kind, on: true, stage: MAKES[kind] ? 'make' : '', name: b.textContent, words: '', label: ''};
    if(MAKES[kind]){ p.text = (B.starters || {})[kind] || ''; p.on = !!p.text; }
    if(kind === 'image') p.count = 1;
    if(kind === 'presenter' && B.presenters[0]) p.presenter = B.presenters[0].id;
    if(kind === 'story'){ p.text = ''; p.style = 'photo'; p.scenes = 5; p.on = false; p.stage = 'make'; }
    S.parts.push(p); S.adding = false; S.editing = MAKES[kind] || kind === 'page' || kind === 'story' ? p.id : '';
    requote().then(function(){ var t = rowsEl.querySelector('[data-id="' + p.id + '"] [data-f]') || rowsEl.querySelector('[data-id="' + p.id + '"] [data-edit-open]'); if(t) t.focus(); });
  });
  $('[data-many]').addEventListener('change', bar);

  // make it
  function ads(){ return S.parts.filter(function(p){ return p.on && MAKES[p.kind]; }).map(function(p){ return {make: p.kind, text: p.text, count: p.count, presenter: p.presenter, situation: p.situation}; }); }
  function run(){
    var Q = S.q || {}; if(Q.short && Q.upgrade && window.cfyUpgrade){ window.cfyUpgrade('That needs ' + credits(Q.total) + ' and you have ' + credits(Q.balance) + '. Pick a plan to keep going.'); return; }
    if(S.busy || !S.q || !S.q.count) return;
    var total = +runBtn.dataset.total, label = runBtn.textContent, set = ads();
    S.busy = true; runBtn.disabled = true; runBtn.textContent = 'Starting'; err('');
    function back(){ S.busy = false; runBtn.textContent = label; bar(); }
    var first = B.claims_check && set.length ? post(B.urls.claims, {plan: set, product: B.product, ask: S.ask}) : Promise.resolve({status: 'off'});
    first.then(function(c){
      if(c && c.error && !c.status){ back(); err(esc(c.error)); return null; }
      if(c && c.status === 'flagged'){ back(); err('These lines say something the product page doesn\'t back up. Edit them and try again: ' + esc((c.flags || []).map(function(f){ return '“' + f.sentence + '”'; }).join(' '))); return null; }
      return post(B.urls.run, {product: B.product, ask: S.ask, parts: S.parts, total: total, confirm_many: $('[data-many]').checked, thread: B.thread, turn: S.turn});
    }).then(function(r){
      if(!r) return;
      if(r.ok){ go(r.url); return; }
      back();
      if(r.card_required){ location.href = r.url; return; }
      if(r.verify && window.cfyVerify && window.CFY_VERIFY){ window.cfyVerify(r.reason).then(function(yes){ if(yes) run(); else err(esc(r.error)); }); return; }
      if(r._status === 409){ requote(); err(esc(r.error || 'The price changed. Check it and confirm again.')); return; }
      err(esc(r.error || 'It couldn\'t start. Try again.') + (r.billing ? ' <a href="' + esc(r.billing) + '">Get credits</a>' : ''));
    }).catch(function(){ back(); err('Something went wrong. Try again.'); });
  }
  runBtn.addEventListener('click', run);

  // the plan as it came with the page
  $('[data-note]').textContent = B.plan.note || '';
  var sk = $('[data-skipped]'); sk.hidden = !(B.plan.skipped || []).length; sk.textContent = (B.plan.skipped || []).length ? 'Left out: ' + B.plan.skipped.join(' ') : '';
  var al = $('[data-also]'); al.hidden = !(B.plan.also || []).length;
  al.innerHTML = (B.plan.also || []).length ? '<span>You also asked for</span>' + B.plan.also.map(function(a){ return '<a href="' + esc(a.href) + '"><b>' + esc(a.name) + '</b><span>' + esc(a.locked ? 'Not in your plan yet' : a.line) + '</span></a>'; }).join('') : '';
  render();
})();
