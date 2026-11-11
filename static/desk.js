/* The campaign desk (desk.py, web/v2/desk.html).
   Plan it -> /app/desk/plan -> rows, each with a price. A tick, an edit, a part added or removed -> /app/desk/quote
   (no model, just the prices again). Make it -> the claims check Studio runs, then /app/desk/run with the total the
   button showed; the server starts nothing when that total is not the one it works out. */
(function(){
  var B = window.CFY_DESK, root = document.querySelector('[data-desk]');
  if(!B || !root || !B.product) return;
  function $(s, el){ return (el || root).querySelector(s); }
  function $$(s, el){ return Array.prototype.slice.call((el || root).querySelectorAll(s)); }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function credits(n){ return Number(n).toLocaleString('en-US') + (Number(n) === 1 ? ' credit' : ' credits'); }
  function post(url, body){
    return fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF': B.csrf, 'X-Requested-With': 'fetch'}, body: JSON.stringify(body)})
      .then(function(r){ return r.json().catch(function(){ return {error: 'Something went wrong. Try again.'}; }).then(function(d){ d._status = r.status; return d; }); });
  }
  function opt(v, label, cur){ return '<option value="' + esc(v) + '"' + (String(cur) === String(v) ? ' selected' : '') + '>' + esc(label) + '</option>'; }

  var MAKES = {image: 1, presenter: 1, voiceover: 1, product_video: 1};
  var STAGES = {make: 'Make', write: 'Write', capture: 'Capture', followup: 'Follow up', launch: 'Launch'};
  var WORDS = {image: 'Headline', presenter: 'What the presenter says', voiceover: 'What the narrator says', product_video: 'The shot'};
  var S = {ask: '', parts: [], q: null, editing: '', adding: false, busy: false, seq: 0, n: 0};

  var ask = $('[data-ask]'), form = $('[data-ask-form]'), sayEl = $('[data-say]'), wrap = $('[data-plan-wrap]');
  var rowsEl = $('[data-rows]'), runBtn = $('[data-run]'), errEl = $('[data-err]'), planBtn = $('[data-plan]');
  function say(msg){ sayEl.hidden = !msg; sayEl.textContent = msg || ''; }
  function err(html){ errEl.hidden = !html; errEl.innerHTML = html || ''; }
  function part(id){ for(var i = 0; i < S.parts.length; i++) if(S.parts[i].id === id) return S.parts[i]; return null; }
  function who(id){ for(var i = 0; i < B.presenters.length; i++) if(B.presenters[i].id === id) return B.presenters[i]; return B.presenters[0] || null; }

  // ---------------------------------------------------------------- the rows
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
    }
    return '<div class="dk-edit" data-edit>' + (h ? '<div class="dk-fields">' + h + '</div>' : '') +
      '<div class="dk-edit-act"><button type="button" class="btn dark sm" data-done>Done</button>' +
      '<button type="button" class="linkbtn" data-remove>Remove this part</button></div></div>';
  }
  function row(p, first){
    var open = S.editing === p.id, can = !p.locked && !p.error && !(MAKES[p.kind] && !p.text);
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
    if(q.short) sum.innerHTML = 'You have ' + esc(credits(q.balance)) + ', so you need ' + esc(credits(q.short)) + ' more. ' + (q.pack ? 'The ' + esc(q.pack.name) + ' pack ($' + esc(q.pack.usd) + ') covers it. ' : '') + '<a href="' + esc(B.urls.billing) + '">Get credits</a>';
    else sum.textContent = (q.count === 1 ? '1 part ticked' : q.count + ' parts ticked') + '. You have ' + credits(q.balance) + '.';
    var many = $('[data-many-wrap]'); many.hidden = !q.confirm;
    if(q.confirm) $('[data-many-text]').textContent = 'Yes, make all ' + q.videos + ' videos.'; else $('[data-many]').checked = false;
    runBtn.textContent = !q.count ? 'Nothing ticked' : q.total ? 'Make it · ' + credits(q.total) : 'Set it up';
    runBtn.disabled = S.busy || !q.count || !!q.short || (!!q.confirm && !$('[data-many]').checked);
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

  // ---------------------------------------------------------------- pricing again, with no model
  function requote(){
    var n = ++S.seq;
    return post(B.urls.quote, {product: B.product, parts: S.parts}).then(function(r){
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
      if(f === 'count' || f === 'n') v = +v;
      if(f === 'presenter' && v !== p.presenter) p.situation = '';
      p[f] = v;
    });
    if(MAKES[p.kind] && p.text && !p.error) p.on = true;
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
    var kind = b.dataset.add, p = {id: 'a' + (++S.n), kind: kind, on: true, stage: MAKES[kind] ? 'make' : '', name: b.textContent, words: '', label: ''};
    if(MAKES[kind]){ p.text = (B.starters || {})[kind] || ''; p.on = !!p.text; }
    if(kind === 'image') p.count = 1;
    if(kind === 'presenter' && B.presenters[0]) p.presenter = B.presenters[0].id;
    S.parts.push(p); S.adding = false; S.editing = MAKES[kind] || kind === 'page' ? p.id : '';
    requote().then(function(){ var t = rowsEl.querySelector('[data-id="' + p.id + '"] [data-f]') || rowsEl.querySelector('[data-id="' + p.id + '"] [data-edit-open]'); if(t) t.focus(); });
  });
  $('[data-many]').addEventListener('change', bar);

  // ---------------------------------------------------------------- the sentence -> the plan
  function planning(on){ S.busy = on; planBtn.disabled = on; planBtn.textContent = on ? 'Planning' : 'Plan it'; $('[data-replan]').disabled = on; root.classList.toggle('dk-busy', on); }
  function plan(){
    var text = ask.value.trim().replace(/\s+/g, ' ');
    if(text.length < 8){ say('Say what you want to launch, in a sentence.'); ask.focus(); return; }
    if(S.busy) return;
    say(''); err(''); planning(true);
    post(B.urls.plan, {ask: text, product: B.product}).then(function(r){
      planning(false);
      if(!r.parts){ say(r.error || 'That didn\'t come back. Try again.'); return; }
      S.ask = r.ask || text; S.parts = r.parts; S.q = r; S.editing = ''; S.adding = false;
      $('[data-note]').textContent = r.note || '';
      var sk = $('[data-skipped]'); sk.hidden = !(r.skipped || []).length; sk.textContent = (r.skipped || []).length ? 'Left out: ' + r.skipped.join(' ') : '';
      var al = $('[data-also]'); al.hidden = !(r.also || []).length;
      al.innerHTML = (r.also || []).length ? '<span>You also asked for</span>' + r.also.map(function(a){ return '<a href="' + esc(a.href) + '"><b>' + esc(a.name) + '</b><span>' + esc(a.locked ? 'Not in your plan yet' : a.line) + '</span></a>'; }).join('') : '';
      var tries = $('[data-tries]'); if(tries) tries.hidden = true;
      wrap.hidden = false; planBtn.classList.add('dark'); render(); $('#dk-plan-h').focus();   // one lime action a screen: now it is Make it
    }).catch(function(){ planning(false); say('That didn\'t come back. Try again.'); });
  }
  form.addEventListener('submit', function(e){ e.preventDefault(); plan(); });
  ask.addEventListener('keydown', function(e){ if(e.key === 'Enter' && (e.metaKey || e.ctrlKey)){ e.preventDefault(); plan(); } });
  $('[data-replan]').addEventListener('click', plan);
  $$('[data-try]').forEach(function(b){ b.addEventListener('click', function(){ ask.value = b.textContent; ask.focus(); }); });

  // ---------------------------------------------------------------- make it
  function ads(){ return S.parts.filter(function(p){ return p.on && MAKES[p.kind]; }).map(function(p){ return {make: p.kind, text: p.text, count: p.count, presenter: p.presenter, situation: p.situation}; }); }
  function run(){
    if(S.busy || !S.q || !S.q.count) return;
    var total = +runBtn.dataset.total, label = runBtn.textContent, plan = ads();
    S.busy = true; runBtn.disabled = true; runBtn.textContent = 'Starting'; err('');
    function back(){ S.busy = false; runBtn.textContent = label; bar(); }
    var first = B.claims_check && plan.length ? post(B.urls.claims, {plan: plan, product: B.product, ask: S.ask}) : Promise.resolve({status: 'off'});
    first.then(function(c){
      if(c && c.error && !c.status){ back(); err(esc(c.error)); return null; }
      if(c && c.status === 'flagged'){ back(); err('These lines say something the product page doesn\'t back up. Edit them and try again: ' + esc((c.flags || []).map(function(f){ return '“' + f.sentence + '”'; }).join(' '))); return null; }
      return post(B.urls.run, {product: B.product, ask: S.ask, parts: S.parts, total: total, confirm_many: $('[data-many]').checked});
    }).then(function(r){
      if(!r) return;
      if(r.ok){ location.href = r.url; return; }
      back();
      if(r.card_required){ location.href = r.url; return; }
      if(r.verify && window.cfyVerify && window.CFY_VERIFY){ window.cfyVerify(r.reason).then(function(yes){ if(yes) run(); else err(esc(r.error)); }); return; }
      if(r._status === 409){ requote(); err(esc(r.error || 'The price changed. Check it and confirm again.')); return; }
      err(esc(r.error || 'It couldn\'t start. Try again.') + (r.billing ? ' <a href="' + esc(r.billing) + '">Get credits</a>' : ''));
    }).catch(function(){ back(); err('Something went wrong. Try again.'); });
  }
  runBtn.addEventListener('click', run);
})();
