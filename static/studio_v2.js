/* Studio composer and Workflow (web/v2/studio.html, web/v2/workflow.html, studio_v2.py).
   Send -> /app/studio/quote -> the price box -> its button -> /app/studio/generate with the total shown.
   Nothing here starts a generation without that box: generate() is only called from the box's button.
   `src` is what Send prices: the composer, or the pack that is open on Workflow.
   Ask (the composer's first tab): Send -> /app/studio/ask -> the same box, on the set that came back. `asked` is
   that set; what is priced and made is the parts still ticked, with the words as they stand in the box. */
(function(){
  var B = window.CFY_STUDIO; if(!B) return;
  var root = document.querySelector('[data-cmp]'), wf = document.querySelector('[data-wf]');
  if(!root && !wf) return;
  function $(s, r){ return (r || document).querySelector(s); }
  function $$(s, r){ return [].slice.call((r || document).querySelectorAll(s)); }
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function credits(n){ n = +n || 0; return n.toLocaleString('en-US') + (n === 1 ? ' credit' : ' credits'); }
  function plural(n, w){ return n + ' ' + w + (n === 1 ? '' : 's'); }
  function post(url, body){
    return fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF': B.csrf, 'X-Requested-With': 'fetch'}, body: JSON.stringify(body)})
      .then(function(r){ return r.json().catch(function(){ return {error: 'Something went wrong. Try again.'}; }).then(function(d){ d._status = r.status; return d; }); });
  }
  function opt(v, label, cur){ return '<option value="' + esc(v) + '"' + (String(cur) === String(v) ? ' selected' : '') + '>' + esc(label) + '</option>'; }
  function words(t){ t = (t || '').trim(); return t ? t.split(/\s+/).length : 0; }
  function fitSeconds(n){ var durs = (B.durations || {}).talk || []; for(var i = 0; i < durs.length; i++){ if(n <= B.words[String(durs[i])]) return durs[i]; } return null; }
  var src = null, lastQuote = null, busy = false;
  var asked = null, qseq = 0;      // asked: {text, product, note, skipped, plan: [{make, text, name, count, on}]}
  function asking(){ return src && src.ask ? src.ask() : null; }
  function askPayload(){
    var plan = (asked ? asked.plan : []).filter(function(it){ return it.on && it.text; }).map(function(it){
      var o = {make: it.make, text: it.text}; if(it.count) o.count = it.count; if(it.presenter) o.presenter = it.presenter; if(it.situation) o.situation = it.situation; return o; });
    return {plan: plan, product: asked ? asked.product : null, ask: asked ? asked.text : ''};
  }
  function payload(){ return asking() && asked ? askPayload() : src.payload(); }
  function say(msg){ var e = src && src.err; if(!e) return; e.hidden = !msg; e.textContent = msg || ''; }

  // ---------------------------------------------------------------- dialogs
  function dlg(name){ return $('[data-dlg="' + name + '"]'); }
  function open(name){ var d = dlg(name); if(d.showModal) d.showModal(); else d.setAttribute('open', ''); return d; }
  function close(d){ if(!d) return; if(d.close) d.close(); else d.removeAttribute('open'); }
  document.addEventListener('click', function(e){
    var x = e.target.closest('[data-close]'); if(x){ close(x.closest('dialog')); return; }
    if(e.target.tagName === 'DIALOG' && e.target.matches('.v2-dlg')){     // a click on the backdrop
      var r = e.target.getBoundingClientRect();
      if(e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) close(e.target);
    }
  });
  // Arrow keys move between the buttons of a radio group or a row of filters (one tab stop for the group).
  function roving(box, sel, onMove){
    box.addEventListener('keydown', function(e){
      if(['ArrowRight', 'ArrowLeft', 'ArrowDown', 'ArrowUp', 'Home', 'End'].indexOf(e.key) < 0) return;
      var t = e.target.closest(sel); if(!t) return;
      var list = $$(sel, t.closest('[role=radiogroup],[role=toolbar]') || box), i = list.indexOf(t); if(i < 0) return;
      e.preventDefault();
      var j = e.key === 'Home' ? 0 : e.key === 'End' ? list.length - 1 : (i + (e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length;
      onMove(list[j]);
    });
  }
  // A row of filter pills is one tab stop, the pressed pill; the others are reached with the arrows.
  function pillStops(row){
    if(!row) return;
    var bs = $$('button', row), on = bs.filter(function(b){ return b.getAttribute('aria-pressed') === 'true'; })[0] || bs[0];
    bs.forEach(function(b){ b.tabIndex = b === on ? 0 : -1; });
  }
  function pillRows(d){ $$('.v2-pills', d).forEach(pillStops); roving(d, '.v2-pills button', function(b){ b.focus(); }); }

  // ================================================================ the composer (Studio)
  if(root) (function(){
    var modeBy = {}; B.modes.forEach(function(m){ modeBy[m.id] = m; });
    if(B.ask_on) modeBy.ask = {id: 'ask', name: 'Ask', noun: 'ask', placeholder: 'Say what you want. For example: three image ads and one short presenter video for the holiday sale'};
    var galBy = {}; B.gallery.forEach(function(x){ galBy[x.id] = x; });
    var presBy = {}; B.presenters.forEach(function(p){ presBy[p.id] = p; });
    var P = B.prefill || {};
    var S = {
      mode: modeBy[B.mode] ? B.mode : B.modes[0].id, product: B.product, text: P.text || '',
      presenters: (P.presenters || []).filter(function(id){ return presBy[id]; }).slice(0, B.max_presenters), situations: {},
      voice: P.voice || '', count: Math.max(1, Math.min(B.max_count, +P.count || 1)),
      duration: P.duration ? String(P.duration) : '', aspect: P.aspect || '', resolution: P.resolution || '720p',
      setting: P.setting || '', scene: P.scene || '', subline: P.subline || '', cta: P.cta || '',
      preset: galBy[P.preset || P.pick] && /^(preset|recipe)$/.test(galBy[P.preset || P.pick].opens) ? galBy[P.preset || P.pick] : null,
      layers: P.layers || {},
      refs: []                                  // brand assets picked as references (brand_assets.py), photo ids
    };
    if(S.preset) S.mode = S.preset.mode;
    else if(B.start === 'ask' && modeBy.ask && !S.text) S.mode = 'ask';
    if(S.preset && S.preset.opens === 'recipe' && !S.text && S.preset.starter) S.text = S.preset.starter;
    if(typeof P.situation === 'string' && P.situation) S.presenters.forEach(function(id){ S.situations[id] = P.situation; });
    else if(P.situation && typeof P.situation === 'object') S.situations = P.situation;
    if(!S.presenters.length && B.presenters.length) S.presenters = [B.presenters[0].id];
    var text = $('[data-text]'), err = $('[data-err]', root);

    function kind(){ return S.mode === 'ask' ? 'ask' : S.mode === 'presenter' || S.mode === 'voiceover' ? 'talk' : S.mode === 'product_video' ? 'video' : 'image'; }
    function product(){ return B.products.filter(function(p){ return p.id === S.product; })[0] || null; }
    function spec(){ return S.preset && S.preset.spec ? S.preset.spec : null; }
    function noHeadline(){ var sp = spec(); return !!(sp && sp.headline === false); }
    // A video preset pinned on a presenter video or a voiceover: it owns the shot, the length it starts at and, for some, the place.
    function recipe(){ return S.preset && S.preset.opens === 'recipe' && S.preset.mode === S.mode ? S.preset : null; }
    function ownPlace(){ var r = recipe(); return (r && r.situation) || ''; }
    // First run: two image ads are two different templates (studio_v2.pair_parts), not two versions of one.
    function pairOn(){ return !!B.pair && S.mode === 'image' && S.count === 2; }
    function situationOf(id){ var p = presBy[id]; if(!p || !p.situations.length) return ''; var s = S.situations[id]; return p.situations.some(function(x){ return x.id === s; }) ? s : p.situations[0].id; }
    function faceOf(p){ if(!p) return ''; var sid = situationOf(p.id), sit = p.situations.filter(function(x){ return x.id === sid; })[0]; return (sit && sit.still) || p.portrait || ''; }

    src = {
      err: err,
      kind: function(){ return kind() === 'ask' ? 'mixed' : kind() === 'image' ? 'image' : 'video'; },
      count: function(){ return S.count; },
      ask: function(){ return S.mode === 'ask' ? {text: text.value.trim().replace(/\s+/g, ' '), product: S.product} : null; },
      check: function(){
        if(!S.product) return {msg: B.products.length ? 'Pick a product first.' : 'Add your product first.', focus: $('[data-product-open]')};
        if(S.mode === 'ask') return text.value.trim().length < 8 ? {msg: 'Say what you want to make, in a sentence.', focus: text} : null;
        if(S.mode === 'presenter' && !S.presenters.length) return {msg: 'Pick a presenter first.', focus: $('[data-presenter-open]')};
        if(!noHeadline() && !text.value.trim()) return {msg: 'Write the ' + modeBy[S.mode].noun + ' first.', focus: text};
        return null;
      },
      payload: function(){
        var d = {mode: S.mode, product: S.product, text: noHeadline() ? '' : text.value.trim(), resolution: S.resolution};
        if(refsOn() && myRefs().length) d.refs = myRefs();
        if(kind() === 'talk'){
          d.duration = S.duration; d.aspect = S.aspect && B.aspects.talk.indexOf(S.aspect) > -1 ? S.aspect : '9:16';
          if(recipe()) d.preset = recipe().id;
          if(S.mode === 'presenter'){
            d.presenters = S.presenters; d.setting = S.setting;
            if(!ownPlace()){              // the place picked with each presenter (a preset with its own place keeps it)
              var sits = {}; S.presenters.forEach(function(id){ var s = situationOf(id); if(s) sits[id] = s; });
              if(S.presenters.length && sits[S.presenters[0]]) d.situation = sits[S.presenters[0]];
              d.situations = sits;
            }
          } else d.voice = S.voice || (B.voices[0] || {}).id;
        } else if(kind() === 'video'){ d.duration = S.duration === '10' ? '10' : '5'; }
        else {
          var own = S.aspect && B.aspects.image.indexOf(S.aspect) > -1 ? S.aspect : '';
          d.count = S.count; d.aspect = own || (S.preset && S.preset.aspect) || '4:5';
          d.scene = S.scene; d.subline = S.subline; d.cta = S.cta;
          if(S.preset && S.preset.opens === 'preset'){ d.preset = S.preset.id; d.layers = S.layers; }
          if(pairOn()){ d.pair = true; d.aspect = own; }      // each look keeps its own shape unless one was chosen
        }
        return d;
      }
    };

    // ---------------------------------------------------------------- the composer
    function thumb(el, url, name, round){
      el.innerHTML = url ? '<img src="' + esc(url) + '" alt="">' : '<b>' + esc((name || '+').slice(0, 1).toUpperCase()) + '</b>';
      el.classList.toggle('round', !!round);
    }
    // References: pictures from the brand (Brand > Assets) put into the ad beside the product photo
    function brandOf(){ var p = product(); return p ? p.brand_id : null; }
    function assets(){ var b = brandOf(); return (B.assets || []).filter(function(a){ return a.brand_id === b && a.product_id !== S.product; }); }   // the product's own photo is in already
    function refsOn(){ return S.mode === 'image' || S.mode === 'presenter'; }
    function myRefs(){ var ok = {}; assets().forEach(function(a){ ok[a.id] = 1; }); return S.refs.filter(function(id){ return ok[id]; }); }
    function refsChip(){
      var b = $('[data-refs-open]'); if(!b) return;
      b.hidden = !refsOn() || !S.product;
      var mine = myRefs(), by = {}; assets().forEach(function(a){ by[a.id] = a; });
      $('[data-refs-thumbs]').innerHTML = mine.slice(0, 3).map(function(id){ return '<img src="' + esc(by[id].url) + '" alt="">'; }).join('');
      $('[data-refs-name]').textContent = mine.length ? mine.length + ' reference' + (mine.length === 1 ? '' : 's') : 'References';
    }
    function refsList(){
      var box = $('[data-refs-list]'), list = assets(), mine = myRefs();
      box.innerHTML = list.length ? list.map(function(a){
        var on = mine.indexOf(a.id) > -1;
        return '<button type="button" aria-pressed="' + on + '" data-ref="' + a.id + '"><img src="' + esc(a.url) + '" alt=""><span><b>' + esc(a.label) + '</b><span class="small muted">' + esc(a.kind_name) + '</span></span></button>';
      }).join('') : '<p class="muted">No pictures yet. Add product photos, your logo, packaging or lifestyle shots on the Brand page.</p>';
      $('[data-refs-n]').textContent = mine.length + ' of ' + B.max_refs;
    }
    var refsBtn = $('[data-refs-open]');
    if(refsBtn){
      refsBtn.addEventListener('click', function(){ refsList(); open('refs'); });
      $('[data-refs-list]').addEventListener('click', function(e){
        var b = e.target.closest('[data-ref]'); if(!b) return;
        var id = +b.dataset.ref, mine = myRefs(), at = mine.indexOf(id);
        if(at > -1) mine.splice(at, 1); else if(mine.length < B.max_refs) mine.push(id); else return;
        S.refs = mine; refsList(); refsChip();
      });
    }

    function render(){
      var m = modeBy[S.mode], k = kind(), sp = spec(), ask = k === 'ask';
      refsChip();
      $$('[data-mode]').forEach(function(b){ var on = b.dataset.mode === S.mode; b.setAttribute('aria-checked', on ? 'true' : 'false'); b.tabIndex = on ? 0 : -1; });
      var label = ask ? 'What you want to make' : sp && sp.items_label ? 'Review' : m.noun.charAt(0).toUpperCase() + m.noun.slice(1);
      $('[data-text-label]').textContent = label;
      text.placeholder = ask ? m.placeholder : noHeadline() ? 'This preset has no words on it' : (sp && sp.items_label ? 'Paste the review exactly as the customer wrote it' : (recipe() && recipe().shape) || m.placeholder);
      text.disabled = !ask && noHeadline();
      root.classList.toggle('asking', ask);
      var so = $('[data-settings-open]'); so.hidden = ask;
      if(ask){ $('[data-settings]').hidden = true; so.setAttribute('aria-expanded', 'false'); }
      $('[data-send]').setAttribute('aria-label', ask ? 'Ask' : 'See the price');
      $('[data-foot]').textContent = ask ? 'Asking is free. You get a set of ads back with the price, and nothing is made until you confirm it.' : 'Send shows the price first. Nothing is made until you confirm it.';
      var pin = $('[data-pin]'); pin.hidden = !S.preset || ask; if(S.preset) $('[data-pin-name]').textContent = S.preset.name;
      var p = product();
      var es = $('[data-empty-step]'), ea = $('[data-empty-add]');
      if(es && p){ es.classList.add('done'); $('b', es).textContent = 'Product added'; $('span', es).textContent = 'Change it any time from the composer.'; if(ea) ea.hidden = true; }
      $('[data-product-name]').textContent = p ? p.name : 'Add product';
      thumb($('[data-product-thumb]'), p && p.photo, p ? p.name : '+');
      var pb = $('[data-presenter-open]'); pb.hidden = S.mode !== 'presenter';
      if(S.mode === 'presenter'){
        var first = presBy[S.presenters[0]];
        $('[data-presenter-name]').textContent = !S.presenters.length ? 'Pick a presenter' : S.presenters.length === 1 ? first.name : plural(S.presenters.length, 'presenter');
        thumb($('[data-presenter-thumb]'), faceOf(first), first ? first.name : '+', true);
      }
      $('[data-voice-wrap]').hidden = S.mode !== 'voiceover';
      $('[data-stepper]').hidden = k !== 'image';
      $('[data-n]').textContent = S.count;
      $('[data-less]').disabled = S.count <= 1; $('[data-plus]').disabled = S.count >= B.max_count;
      var ph = $('[data-pair-hint]');
      ph.hidden = !pairOn();
      if(pairOn()) ph.textContent = (!S.preset && S.product === B.product && (B.pair_names || []).length === 2 ? 'Two different looks: ' + B.pair_names.join(' and ') + '. ' : 'Two different looks, picked for your product. ');
      count(); fields(); if(!$('[data-settings]').hidden) settings();
    }
    function count(){
      var el = $('[data-count]'), n = words(text.value);
      if(kind() !== 'talk'){ el.textContent = ''; return; }
      var d = S.duration ? +S.duration : fitSeconds(n);
      if(d && !S.duration && recipe() && recipe().duration > d) d = recipe().duration;       // a preset's own length is the least
      el.textContent = !n ? '' : plural(n, 'word') + (d ? ', ' + d + ' s' : ', too long');
      el.classList.toggle('over', !!n && (!d || n > B.words[String(d)]));
    }

    // The extra words a text-layer preset asks for (presets.layout_spec).
    function fields(){
      var box = $('[data-fields]'), sp = spec();
      if(S.mode !== 'image' || !sp || !sp.fields.length){ box.hidden = true; box.innerHTML = ''; box.dataset.for = ''; return; }
      if(box.dataset.for === S.preset.id) return;
      box.dataset.for = S.preset.id; box.hidden = false;
      var p = product();
      box.innerHTML = sp.fields.map(function(f){
        var id = 'lf-' + f.id, v = S.layers[f.id], hint = f.hint ? '<span class="cmp-hint">' + esc(f.hint) + '</span>' : '';
        if(f.type === 'confirm') return '<label class="cmp-f check"><input type="checkbox" data-layer="' + f.id + '" data-lt="confirm"' + (v ? ' checked' : '') + '> <span>' + esc(f.label) + '</span></label>';
        if(f.type === 'stars') return '<div class="cmp-f"><label for="' + id + '">' + esc(f.label) + '</label><select id="' + id + '" data-layer="' + f.id + '" data-lt="stars"><option value="0">Not shown</option>' + [5, 4, 3, 2, 1].map(function(n){ return '<option value="' + n + '"' + (+v === n ? ' selected' : '') + '>' + n + '</option>'; }).join('') + '</select></div>';
        if(f.type === 'list'){
          var vals = Array.isArray(v) ? v : [], out = '<fieldset class="cmp-f list"><legend>' + esc(f.label) + (f.required ? '' : ' (optional)') + '</legend>';
          for(var i = 0; i < (f.n || 4); i++) out += '<input type="text" maxlength="' + (f.max || 60) + '" aria-label="' + esc(f.label) + ' ' + (i + 1) + '" data-layer="' + f.id + '" data-lt="list" data-i="' + i + '" value="' + esc(vals[i] || '') + '">';
          return out + hint + '</fieldset>';
        }
        var dv = v != null ? v : (f.id === 'badge' && p ? (p.offer || p.price) : '');
        if(v == null && dv) S.layers[f.id] = dv;
        return '<div class="cmp-f"><label for="' + id + '">' + esc(f.label) + (f.required ? '' : ' (optional)') + '</label><input id="' + id + '" type="text" maxlength="' + (f.max || 60) + '" data-layer="' + f.id + '" data-lt="text" value="' + esc(dv || '') + '" placeholder="' + esc(String(f['default'] || '').replace('{product}', p ? p.name : 'Your product')) + '">' + hint + '</div>';
      }).join('');
    }
    function readLayer(el){
      var id = el.dataset.layer, t = el.dataset.lt;
      if(t === 'confirm') S.layers[id] = el.checked;
      else if(t === 'stars') S.layers[id] = +el.value;
      else if(t === 'list'){ S.layers[id] = $$('[data-layer="' + id + '"]', root).map(function(x){ return x.value.trim(); }).filter(Boolean); }
      else S.layers[id] = el.value;
    }

    function settings(){
      var box = $('[data-settings]'), k = kind(), h = '', sp = spec(), layers = sp ? sp.layers : null;
      if(k === 'talk'){
        h += '<div class="cmp-f"><label for="set-dur">Length</label><select id="set-dur" data-set="duration">' + opt('', 'Auto: fits the script', S.duration) + B.durations.talk.map(function(d){ return opt(d, d + ' seconds', S.duration); }).join('') + '</select></div>';
        h += '<div class="cmp-f"><label for="set-asp">Shape</label><select id="set-asp" data-set="aspect">' + B.aspects.talk.map(function(a){ return opt(a, a, S.aspect || '9:16'); }).join('') + '</select></div>';
        if(B.resolutions.talk.length > 1) h += '<div class="cmp-f"><label for="set-res">Quality</label><select id="set-res" data-set="resolution">' + B.resolutions.talk.map(function(r){ return opt(r, r, S.resolution); }).join('') + '</select></div>';
        if(S.mode === 'presenter') h += '<div class="cmp-f wide"><label for="set-where">Where they are (optional)</label><input id="set-where" type="text" maxlength="120" data-set="setting" value="' + esc(S.setting) + '" placeholder="Default: the situation picked with the presenter"></div>';
      } else if(k === 'video'){
        h += '<div class="cmp-f"><label for="set-dur">Length</label><select id="set-dur" data-set="duration">' + B.durations.video.map(function(d){ return opt(d, d + ' seconds', S.duration || '5'); }).join('') + '</select></div>';
        if(B.resolutions.video.length > 1) h += '<div class="cmp-f"><label for="set-res">Quality</label><select id="set-res" data-set="resolution">' + B.resolutions.video.map(function(r){ return opt(r, r, S.resolution); }).join('') + '</select></div>';
      } else {
        h += '<div class="cmp-f"><label for="set-asp">Shape</label><select id="set-asp" data-set="aspect">' + (pairOn() ? opt('', 'Each look\'s own shape', S.aspect) : '') + B.aspects.image.map(function(a){ return opt(a, a, S.aspect || (pairOn() ? '' : (S.preset && S.preset.aspect) || '4:5')); }).join('') + '</select></div>';
        if(!layers || layers.indexOf('subhead') > -1) h += '<div class="cmp-f"><label for="set-sub">Subline (optional)</label><input id="set-sub" type="text" maxlength="140" data-set="subline" value="' + esc(S.subline) + '"></div>';
        if(!layers || layers.indexOf('cta') > -1) h += '<div class="cmp-f"><label for="set-cta">Button text (optional)</label><input id="set-cta" type="text" maxlength="30" data-set="cta" value="' + esc(S.cta) + '" placeholder="Shop now"></div>';
        h += '<div class="cmp-f wide"><label for="set-scene">Scene (optional)</label><textarea id="set-scene" rows="2" maxlength="2000" data-set="scene" placeholder="Where the product sits, the light, the mood">' + esc(S.scene) + '</textarea></div>';
      }
      box.innerHTML = h;
    }

    function setMode(id, keepPreset){
      if(!modeBy[id]) return;
      S.mode = id; if(!keepPreset && S.preset && S.preset.mode !== id) S.preset = null;
      if(id !== 'image'){ $('[data-fields]').dataset.for = ''; }
      say(''); render();
    }

    // products
    function productList(){
      var box = $('[data-plist]');
      box.innerHTML = B.products.length ? B.products.map(function(p){
        return '<button type="button" role="radio" aria-checked="' + (p.id === S.product) + '" data-pid="' + p.id + '"><span class="cmp-thumb">' + (p.photo ? '<img src="' + esc(p.photo) + '" alt="">' : '<b>' + esc(p.name.slice(0, 1).toUpperCase()) + '</b>') + '</span><span><b>' + esc(p.name) + '</b><span class="small muted">' + esc([p.brand, p.price].filter(Boolean).join(', ')) + (p.photo ? '' : (p.brand || p.price ? ', ' : '') + 'no photo yet') + '</span></span></button>';
      }).join('') : '<p class="muted v2-plist-none">No products yet. Paste the link to your product page below.</p>';
    }
    function openProducts(){ productList(); var d = open('product'); var cur = $('[aria-checked="true"]', d) || $('[data-import-url]', d); if(cur) cur.focus(); }
    $('[data-product-open]').addEventListener('click', openProducts);
    var emptyAdd = $('[data-empty-add]'); if(emptyAdd) emptyAdd.addEventListener('click', openProducts);
    $('[data-plist]').addEventListener('click', function(e){ var b = e.target.closest('[data-pid]'); if(!b) return; S.product = +b.dataset.pid; $('[data-fields]').dataset.for = ''; close(dlg('product')); render(); text.focus(); });
    $('[data-import]').addEventListener('submit', function(e){
      e.preventDefault();
      var url = $('[data-import-url]').value.trim(), msg = $('[data-import-msg]'), go = $('[data-import-go]'); if(!url) return;
      go.disabled = true; go.textContent = 'Reading the page'; msg.textContent = 'This takes a few seconds.'; msg.classList.remove('bad');
      post(B.urls['import'], {url: url}).then(function(r){
        go.disabled = false; go.textContent = 'Add';
        if(!r.ok){ msg.textContent = r.error || 'That page could not be read. Add the product by hand.'; msg.classList.add('bad'); return; }
        B.products = B.products.filter(function(p){ return p.id !== r.product_id; });
        B.products.unshift({id: r.product_id, name: r.name || 'Your product', brand: r.brand || '', photo: r.photo ? r.photo.url : '', price: '', offer: ''});
        S.product = r.product_id; $('[data-import-url]').value = ''; msg.textContent = 'We read the name, brand and photo from the page. It costs nothing.';
        $$('[data-sw-open] .sw-txt b').forEach(function(b){ b.textContent = r.name || 'Your product'; });       // the sidebar's switcher shows it too
        if(r.brand) $$('[data-sw-open] .sw-brand').forEach(function(b){ b.textContent = r.brand; });
        close(dlg('product')); render(); text.focus();
      }).catch(function(){ go.disabled = false; go.textContent = 'Add'; msg.textContent = 'Something went wrong. Try again.'; msg.classList.add('bad'); });
    });

    // ---------------------------------------------------------------- presenters: portraits, situations, filters, voice
    var PF = {gender: '', age_band: ''}, FILTERS = [['gender', {female: 'Women', male: 'Men', nonbinary: 'Nonbinary'}], ['age_band', {}]];
    var player = null, playing = '';
    function stopVoice(){ if(player){ player.pause(); } playing = ''; $$('[data-voice-play]').forEach(function(b){ b.setAttribute('aria-pressed', 'false'); }); }
    function playVoice(id){
      var p = presBy[id]; if(!p || !p.voice_sample) return;
      if(playing === id){ stopVoice(); return; }
      stopVoice();
      if(!player){ player = new Audio(); player.preload = 'none'; player.addEventListener('ended', stopVoice); player.addEventListener('error', stopVoice); }
      player.src = p.voice_sample; playing = id;
      var b = $('[data-voice-play="' + id + '"]'); if(b) b.setAttribute('aria-pressed', 'true');
      var pr = player.play(); if(pr && pr.catch) pr.catch(stopVoice);
    }
    function label(v){ return v.charAt(0).toUpperCase() + v.slice(1); }
    function presenterFilters(){
      FILTERS.forEach(function(f){
        var key = f[0], names = f[1], vals = [];
        B.presenters.forEach(function(p){ if(p[key] && vals.indexOf(p[key]) < 0) vals.push(p[key]); });
        if(vals.every(function(v){ return /^\d/.test(v); })) vals.sort(function(a, b){ return parseInt(a, 10) - parseInt(b, 10); });
        var grp = $('[data-pf-group="' + key + '"]'); grp.hidden = vals.length < 2;
        $('[data-pf-pills="' + key + '"]').innerHTML = '<button type="button" aria-pressed="' + (!PF[key]) + '" data-pf="' + key + '" data-pv="">All</button>' + vals.map(function(v){
          return '<button type="button" aria-pressed="' + (PF[key] === v) + '" data-pf="' + key + '" data-pv="' + esc(v) + '">' + esc(names[v] || label(v)) + '</button>'; }).join('');
        pillStops($('[data-pf-pills="' + key + '"]'));
      });
    }
    function presenterGrid(){
      var box = $('[data-pgrid]'), full = S.presenters.length >= B.max_presenters, n = 0;
      box.innerHTML = B.presenters.filter(function(p){ return (!PF.gender || p.gender === PF.gender) && (!PF.age_band || p.age_band === PF.age_band); }).map(function(p){
        n++;
        var on = S.presenters.indexOf(p.id) > -1, sid = situationOf(p.id), face = faceOf(p), meta = [p.age_band, p.vibe].filter(Boolean).join(', ');
        var h = '<div class="v2-pcard' + (on ? ' on' : '') + '" data-card="' + esc(p.id) + '">' +
          '<button type="button" class="v2-psel" role="checkbox" aria-checked="' + on + '" data-pres="' + esc(p.id) + '"' + (!on && full ? ' aria-disabled="true"' : '') + '>' +
          '<span class="v2-portrait">' + (face ? '<img src="' + esc(face) + '" alt="" loading="lazy">' : '<b aria-hidden="true">' + esc(p.name.slice(0, 1)) + '</b><span class="v2-look">' + esc(p.look) + '</span>') + '<i class="v2-tick" aria-hidden="true"></i></span>' +
          '<span class="v2-pname"><b>' + esc(p.name) + '</b>' + (meta ? '<span>' + esc(meta) + '</span>' : '') + '</span></button>';
        h += '<div class="v2-pmeta">' + (p.voice_sample ? '<button type="button" class="v2-play" data-voice-play="' + esc(p.id) + '" aria-pressed="' + (playing === p.id) + '" aria-label="Play ' + esc(p.name) + '\'s voice"><svg class="pl" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z" fill="currentColor"/></svg><svg class="pa" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5.5h3.5v13H7zM13.5 5.5H17v13h-3.5z" fill="currentColor"/></svg></button>' : '') +
          '<span class="v2-pvoice">' + (p.voice.label ? 'Voice: ' + esc(p.voice.label) : 'Voice set with the presenter') + '</span></div>';
        if(ownPlace()){ h += '<p class="v2-sit1">' + esc((B.situation_labels || {})[ownPlace()] || ownPlace()) + ', set by the preset</p>'; }
        else if(p.situations.length > 1){
          h += '<div class="v2-sits" role="radiogroup" aria-label="Where ' + esc(p.name) + ' is">' + p.situations.map(function(s){
            var cur = s.id === sid;
            return '<button type="button" role="radio" aria-checked="' + cur + '" tabindex="' + (cur ? 0 : -1) + '" data-sit="' + esc(s.id) + '" data-for="' + esc(p.id) + '">' + (s.still ? '<img src="' + esc(s.still) + '" alt="" loading="lazy">' : '') + '<span>' + esc(s.label) + '</span></button>'; }).join('') + '</div>';
        } else if(p.situations.length === 1){ h += '<p class="v2-sit1">' + esc(p.situations[0].label) + '</p>'; }
        return h + '</div>';
      }).join('');
      box.classList.toggle('nofaces', !B.presenters.some(function(p){ return faceOf(p); }));
      $('[data-pres-none]').hidden = n > 0;
      $('[data-pres-n]').textContent = S.presenters.length + ' of ' + B.max_presenters + ' picked';
    }
    function togglePresenter(id, forceOn){
      var i = S.presenters.indexOf(id);
      if(i > -1){ if(!forceOn && S.presenters.length > 1) S.presenters.splice(i, 1); }
      else if(S.presenters.length < B.max_presenters) S.presenters.push(id);
    }
    $('[data-presenter-open]').addEventListener('click', function(){ presenterFilters(); presenterGrid(); var d = open('presenter'); var f = $('[data-pres][aria-checked="true"]', d) || $('[data-pres]', d); if(f) f.focus(); });
    dlg('presenter').addEventListener('close', stopVoice);
    dlg('presenter').addEventListener('click', function(e){
      var f = e.target.closest('[data-pf]');
      if(f){ PF[f.dataset.pf] = f.dataset.pv; presenterFilters(); presenterGrid(); var again = $('[data-pf="' + f.dataset.pf + '"][data-pv="' + f.dataset.pv + '"]'); if(again) again.focus(); return; }
      var v = e.target.closest('[data-voice-play]'); if(v){ playVoice(v.dataset.voicePlay); return; }
      var s = e.target.closest('[data-sit]');
      if(s){ S.situations[s.dataset['for']] = s.dataset.sit; togglePresenter(s.dataset['for'], true); presenterGrid(); var sb = $('[data-sit="' + s.dataset.sit + '"][data-for="' + s.dataset['for'] + '"]'); if(sb) sb.focus(); render(); return; }
      var c = e.target.closest('[data-pres]'); if(!c || c.getAttribute('aria-disabled') === 'true') return;
      togglePresenter(c.dataset.pres); presenterGrid(); var pb = $('[data-pres="' + c.dataset.pres + '"]'); if(pb) pb.focus(); render();
    });
    roving(dlg('presenter'), '[data-sit]', function(b){ b.click(); });
    roving(dlg('presenter'), '.v2-pills button', function(b){ b.focus(); });

    // ---------------------------------------------------------------- more: ad types and presets
    var galKind = '', galUse = '', useLabel = {}; (B.gallery_uses || []).forEach(function(u){ useLabel[u.id] = u.label; });
    function galleryGrid(){
      var q = $('[data-gal-q]').value.trim().toLowerCase(), n = 0;
      $('[data-gal]').innerHTML = B.gallery.filter(function(x){
        var words = x.name + ' ' + x.what_for + ' ' + x.badge + ' ' + (x.uses || []).map(function(u){ return useLabel[u] || u; }).join(' ');
        return (!galKind || x.group === galKind) && (!galUse || (x.uses || []).indexOf(galUse) > -1) && (!q || words.toLowerCase().indexOf(q) > -1);
      }).map(function(x){ n++;
        var media = x.video ? '<video src="' + esc(x.video) + '" poster="' + esc(x.preview) + '" muted loop playsinline preload="none" data-hover></video>' : '<img src="' + esc(x.preview) + '" alt="" loading="lazy">';
        return '<button type="button" class="v2-gcard" data-gal-id="' + esc(x.id) + '"><span class="v2-gthumb' + (x.example ? ' real' : '') + '">' + media + '</span><span class="v2-gbody"><span class="v2-badge">' + esc(x.badge) + '</span><b>' + esc(x.name) + '</b><span class="v2-gline">' + esc(x.what_for) + '</span><span class="v2-gfrom">' + (x.opens === 'advanced' ? 'Opens in Advanced, ' : '') + 'from ' + credits(x.from) + '</span></span></button>';
      }).join('');
      $('[data-gal-none]').hidden = n > 0;
    }
    pillRows(dlg('more'));
    $('[data-more-open]').addEventListener('click', function(){ galleryGrid(); open('more'); $('[data-gal-q]').focus(); });
    $('[data-gal-q]').addEventListener('input', galleryGrid);
    var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    function hoverPlay(e){ var v = e.target.closest && e.target.closest('.v2-gcard'); v = v && $('video[data-hover]', v); if(!v || still) return; if(e.type === 'mouseover' || e.type === 'focusin'){ var p = v.play(); if(p && p.catch) p.catch(function(){}); } else v.pause(); }
    ['mouseover', 'mouseout', 'focusin', 'focusout'].forEach(function(t){ dlg('more').addEventListener(t, hoverPlay); });
    dlg('more').addEventListener('click', function(e){
      var f = e.target.closest('[data-gf]'), u = e.target.closest('[data-gu]');
      if(f){ galKind = f.dataset.gf; $$('[data-gf]').forEach(function(b){ b.setAttribute('aria-pressed', b === f ? 'true' : 'false'); }); pillStops(f.parentNode); galleryGrid(); return; }
      if(u){ galUse = u.dataset.gu; $$('[data-gu]').forEach(function(b){ b.setAttribute('aria-pressed', b === u ? 'true' : 'false'); }); pillStops(u.parentNode); galleryGrid(); return; }
      var c = e.target.closest('[data-gal-id]'); if(!c) return;
      pick(galBy[c.dataset.galId]); close(dlg('more')); text.focus();
    });
    function pick(x){
      if(!x) return;
      if(x.opens === 'advanced'){ location.href = x.href; return; }
      if(x.opens === 'mode'){ setMode(x.mode); return; }
      if(x.opens === 'scene'){ S.duration = String(x.duration || 5); setMode('product_video'); if(x.scene) text.value = x.scene; render(); return; }
      if(x.opens === 'recipe'){ S.preset = x; S.duration = ''; if(x.starter && !text.value.trim()) text.value = x.starter; setMode(x.mode, true); return; }
      S.preset = x; S.layers = {}; S.aspect = x.aspect || S.aspect; $('[data-fields]').dataset.for = '';
      if(x.scene) S.scene = x.scene;
      if(x.headline && !text.value.trim()) text.value = x.headline;
      setMode('image', true);
    }

    // ---------------------------------------------------------------- composer events
    root.addEventListener('click', function(e){
      var m = e.target.closest('[data-mode]'); if(m){ setMode(m.dataset.mode); return; }
      if(e.target.closest('[data-pin-x]')){ S.preset = null; S.layers = {}; render(); return; }
      if(e.target.closest('[data-less]')){ S.count = Math.max(1, S.count - 1); render(); return; }
      if(e.target.closest('[data-plus]')){ S.count = Math.min(B.max_count, S.count + 1); render(); return; }
      var so = e.target.closest('[data-settings-open]');
      if(so){ var box = $('[data-settings]'); box.hidden = !box.hidden; so.setAttribute('aria-expanded', box.hidden ? 'false' : 'true'); if(!box.hidden){ settings(); var f = $('select, input, textarea', box); if(f) f.focus(); } return; }
      if(e.target.closest('[data-send]')) send();
    });
    roving($('[data-modes]'), '[data-mode]', function(b){ setMode(b.dataset.mode); $('[data-mode="' + b.dataset.mode + '"]').focus(); });
    root.addEventListener('input', function(e){
      var t = e.target;
      if(t === text){ count(); say(''); var al = $('[data-ask-also]'); if(al) al.hidden = true; return; }
      if(t.dataset.layer){ readLayer(t); return; }
      if(t.dataset.set){ S[t.dataset.set] = t.value; if(t.dataset.set === 'duration') count(); }
    });
    root.addEventListener('change', function(e){ if(e.target.matches('[data-voice]')) S.voice = e.target.value; if(e.target.dataset.layer) readLayer(e.target); });
    text.addEventListener('keydown', function(e){ if(e.key === 'Enter' && (e.metaKey || e.ctrlKey)){ e.preventDefault(); send(); } });

    // ---------------------------------------------------------------- start
    var vs = $('[data-voice]');
    vs.innerHTML = B.voices.map(function(v){ return opt(v.id, v.name, S.voice); }).join('');
    if(!S.voice && B.voices.length){ S.voice = B.voices.some(function(v){ return v.id === 'narrator_deep'; }) ? 'narrator_deep' : vs.value; vs.value = S.voice; }
    if(S.text) text.value = S.text;
    render();
    var hl = document.querySelector('.litem.hl'); if(hl && hl.scrollIntoView) hl.scrollIntoView({block: 'nearest'});
  })();

  // ================================================================ Workflow: the planner box and the packs
  if(wf) (function(){
    var packBy = {}; (B.packs || []).forEach(function(p){ packBy[p.id] = p; });
    var cur = null, form = $('[data-pack-form]'), box = $('[data-pack-fields]');
    function field(id){ return $('[data-pf-id="' + id + '"]', box); }
    function scriptHint(){
      var el = field('script'), out = $('[data-script-n]', box); if(!el || !out) return;
      var n = words(el.value), d = fitSeconds(n);
      out.textContent = !n ? '' : plural(n, 'word') + (d ? ', a ' + d + '-second video' : ', too long for one video');
      out.classList.toggle('over', !!n && !d);
    }
    function openPack(id){
      cur = packBy[id]; if(!cur) return;
      $('[data-pack-title]').textContent = cur.name;
      $('[data-pack-line]').textContent = cur.line;
      $('[data-pack-inside]').innerHTML = cur.inside.map(function(it){ return '<li><i class="wf-shape a' + esc(it.aspect.replace(':', 'x')) + (it.kind === 'video' ? ' vid' : '') + '" aria-hidden="true"></i><span><b>' + esc(it.name) + '</b><span>' + (it.kind === 'video' ? 'Video' : 'Image') + ', ' + esc(it.aspect) + '</span></span></li>'; }).join('');
      box.innerHTML = cur.fields.map(function(f){
        var id = 'pk-' + f.id, v = (B.prefill || {})[f.id] || '';
        var input = f.id === 'script' ? '<textarea id="' + id + '" rows="4" maxlength="' + f.max + '" data-pf-id="' + f.id + '">' + esc(v) + '</textarea>'
          : '<input id="' + id + '" type="text" maxlength="' + f.max + '" data-pf-id="' + f.id + '" value="' + esc(v) + '">';
        var extra = f.id === 'script' ? '<span class="cmp-count" data-script-n aria-live="polite"></span>' : '';
        return '<div class="cmp-f"><label for="' + id + '">' + esc(f.label) + '</label>' + input + '<span class="cmp-hint">' + esc(f.hint) + ' ' + extra + '</span></div>';
      }).join('') + (cur.fields.some(function(f){ return f.id === 'script'; }) ? '<div class="cmp-f"><label for="pk-voice">Narrator voice</label><select id="pk-voice" data-pack-voice>' + B.voices.map(function(v){ return opt(v.id, v.name, 'narrator_deep'); }).join('') + '</select></div>' : '');
      src = {
        err: $('[data-err]', form),
        kind: function(){ return cur.inside.some(function(it){ return it.kind === 'video'; }) ? 'mixed' : 'image'; },
        count: function(){ return 1; },
        check: function(){
          for(var i = 0; i < cur.fields.length; i++){ var el = field(cur.fields[i].id); if(el && !el.value.trim()) return {msg: 'Write the ' + cur.fields[i].label.toLowerCase() + ' first.', focus: el}; }
          return null;
        },
        payload: function(){
          var f = {}; cur.fields.forEach(function(x){ var el = field(x.id); f[x.id] = el ? el.value.trim() : ''; });
          var v = $('[data-pack-voice]', box);
          return {pack: cur.id, product: B.product, fields: f, voice: v ? v.value : ''};
        }
      };
      say(''); scriptHint();
      var d = open('pack'), first = $('input, textarea', box); if(first) first.focus(); return d;
    }
    wf.addEventListener('click', function(e){ var p = e.target.closest('[data-pack]'); if(p && !p.disabled) openPack(p.dataset.pack); });
    box.addEventListener('input', function(e){ if(e.target.dataset.pfId === 'script') scriptHint(); say(''); });
    form.addEventListener('submit', function(e){ e.preventDefault(); send(); });

    // the planner: its price note first, then its own form is sent as it is
    var pf = $('[data-plan-form]'), notes = $('[data-plan-notes]'), perr = $('[data-plan-err]');
    function planSay(msg){ perr.hidden = !msg; perr.textContent = msg || ''; }
    var po = $('[data-plan-open]');
    if(po) po.addEventListener('click', function(){
      if(!notes.value.trim()){ planSay('Describe what you want first.'); notes.focus(); return; }
      planSay(''); var d = open('plan'); var go = $('[data-plan-go]', d) || $('a.btn', d); if(go) go.focus();
    });
    notes.addEventListener('input', function(){ planSay(''); });
    notes.addEventListener('keydown', function(e){ if(e.key === 'Enter' && (e.metaKey || e.ctrlKey) && po){ e.preventDefault(); po.click(); } });
    var pg = $('[data-plan-go]'); if(pg) pg.addEventListener('click', function(){ pg.disabled = true; pg.textContent = 'Starting'; pf.submit(); });
    var full = $('[data-plan-full]'); if(full) full.addEventListener('click', function(){ if(notes.value.trim()) full.href = full.href.split('?')[0] + '?notes=' + encodeURIComponent(notes.value.trim().slice(0, 1500)); });
  })();

  // ---------------------------------------------------------------- price, then confirm, then make
  function working(on){ busy = on; $$('[data-send]').forEach(function(b){ b.disabled = on; b.classList.toggle('busy', on); }); }
  function send(){
    if(busy || !src) return;
    var stop = src.check();
    if(stop){ say(stop.msg); if(stop.focus) stop.focus.focus(); return; }
    var a = asking();
    if(a){ if(asked && asked.text === a.text && asked.product === a.product) requote(); else ask(a); return; }
    say(''); working(true);
    post(B.urls.quote, payload()).then(function(q){
      working(false);
      if(q.card_required){ location.href = q.url; return; }
      if(q.error || !q.count){ say(q.error || 'That can\'t be priced. Check the settings and try again.'); return; }
      lastQuote = q; showPrice(q);
    }).catch(function(){ working(false); say('The price didn\'t load. Try again.'); });
  }
  // ---------------------------------------------------------------- ask: a sentence in, a priced set back
  // Where the other things the sentence asked for live (studio_v2.ask_also): links, never a spend.
  function alsoRows(el, list){
    if(!el) return;
    el.hidden = !(list && list.length);
    el.innerHTML = (list || []).map(function(a){ return '<li><span><b>' + esc(a.name) + '</b><span class="small muted">' + esc(a.line) + (a.locked ? ' On a paid plan.' : '') + '</span></span><a class="btn dark sm" href="' + esc(a.href) + '">Open</a></li>'; }).join('');
  }
  function waiting(on){ var w = $('[data-asking]'); if(w) w.hidden = !on; var b = $('[data-q-again]'); if(b) b.disabled = on; }
  function ask(a){
    var inBox = dlg('price').open, fail = inBox ? qerr : say;
    say(''); working(true); waiting(true);
    post(B.urls.ask, {ask: a.text, product: a.product}).then(function(r){
      working(false); waiting(false);
      alsoRows($('[data-ask-also]'), r.plan ? [] : r.also);
      if(r.error || !r.plan || !r.quote){ fail(r.error || 'That didn\'t come back. Try again.'); return; }
      asked = {also: r.also || [], text: a.text, product: r.product, note: r.note || '', skipped: r.skipped || [], plan: r.plan.map(function(it){ it.on = true; return it; })};
      planRows(); lastQuote = r.quote; showPrice(r.quote);
      if(inBox) $$('[data-plan-text]').forEach(fit);
    }).catch(function(){ working(false); waiting(false); fail('That didn\'t come back. Try again.'); });
  }
  function planRows(){
    $('[data-q-asknote]').textContent = asked.note || 'Here is a set for that.';
    $('[data-q-skipped]').textContent = asked.skipped.length ? 'Left out: ' + asked.skipped.join(' ') : '';
    alsoRows($('[data-q-also]'), asked.also);
    $('[data-q-plan]').innerHTML = asked.plan.map(function(it, i){
      var what = it.make === 'image' ? 'Headline' : it.make === 'product_video' ? 'Shot' : 'Script';
      return '<li data-plan-row="' + i + '"><input type="checkbox" checked data-plan-on="' + i + '" aria-label="Make this: ' + esc(it.name) + '">' +
        '<b>' + esc(it.name) + '</b><span class="v2-plan-cr" data-plan-cr="' + i + '"></span>' +
        '<textarea rows="1" maxlength="900" data-plan-text="' + i + '" aria-label="' + what + ' for ' + esc(it.name) + '">' + esc(it.text) + '</textarea></li>';
    }).join('');
  }
  // A part's words show in full: the box is as tall as what is in it (set once the dialog is laid out, and as it is typed in).
  function fit(el){ el.style.height = 'auto'; el.style.height = (el.scrollHeight + 2) + 'px'; }
  // Each part's price, from the quote of the parts still ticked (they are quoted in the order shown).
  function planPrices(q){
    var j = 0;
    asked.plan.forEach(function(it, i){
      var row = $('[data-plan-row="' + i + '"]'), on = it.on && !!it.text;
      row.classList.toggle('off', !on);
      $('[data-plan-cr="' + i + '"]').textContent = on ? credits((q.parts || [])[j++]) : 'Not made';
    });
  }
  function requote(){
    var p = askPayload(), n = ++qseq, inBox = dlg('price').open, fail = inBox ? qerr : say, ok = $('[data-confirm-make]');
    if(!p.plan.length){ lastQuote = {count: 0, ads: 0, total: 0, lines: [], parts: [], balance: (lastQuote || {}).balance || B.balance || 0, short: 0}; showPrice(lastQuote); return; }
    ok.disabled = true;
    post(B.urls.quote, p).then(function(q){
      if(n !== qseq) return;                    // a newer tick or edit is already being priced
      if(q.error || !q.count){ fail(q.error || 'That can\'t be priced. Change it and try again.'); return; }
      lastQuote = q; showPrice(q);
    }).catch(function(){ if(n === qseq) fail('The price didn\'t load. Try again.'); });
  }
  dlg('price').addEventListener('change', function(e){
    var t = e.target;
    if(!asked) return;
    if(t.dataset.planOn != null){ asked.plan[+t.dataset.planOn].on = t.checked; requote(); }
    else if(t.dataset.planText != null){ asked.plan[+t.dataset.planText].text = t.value.trim().replace(/\s+/g, ' '); requote(); }
  });
  dlg('price').addEventListener('input', function(e){ if(e.target.dataset.planText != null) fit(e.target); });
  var again = $('[data-q-again]'); if(again) again.addEventListener('click', function(){ var a = asking(); if(a && !busy){ qerr(''); ask(a); } });

  function showPrice(q){
    var d = dlg('price'), k = src.kind(), noun = k === 'image' ? 'image ad' : k === 'mixed' ? 'ad' : 'video', n = q.ads || q.count * (k === 'image' ? src.count() : 1);
    var set = !!(asking() && asked);
    d.classList.toggle('asked', set); $('[data-q-ask]').hidden = !set; $('[data-q-lines]').hidden = set;
    $('[data-q-title]').textContent = n ? 'Make ' + plural(n, noun) + ' for ' + credits(q.total) + '?' : 'Tick at least one thing to make';
    if(set) planPrices(q);
    else $('[data-q-lines]').innerHTML = q.lines.filter(function(l){ return l.on; }).map(function(l){ return '<li><span><b>' + esc(l.label) + '</b><span class="small muted">' + esc(l.hook) + '</span></span><span>' + credits(l.credits) + '</span></li>'; }).join('');
    $('[data-q-total]').textContent = credits(q.total);
    $('[data-q-bal]').textContent = credits(q.balance);
    $('[data-q-after]').textContent = q.short ? 'Not enough' : credits(q.balance - q.total);
    var note = $('[data-q-note]'), notes = [];
    if(B.first_run && !q.short && k === 'image') notes.push(B.plans_only ? 'Your credits cover this.' : 'Your free credits cover this.');
    if(B.claims_check) notes.push('The words are checked against your product\'s claims and page first' + (B.claims_credits ? ' (about ' + credits(B.claims_credits) + ').' : ', free.'));
    if(q.videos) notes.push('Failed videos are refunded automatically.');
    note.hidden = !notes.length; note.textContent = notes.join(' ');
    var many = $('[data-q-many-wrap]'); many.hidden = !q.confirm; $('[data-q-many]').checked = false;
    if(q.confirm) $('[data-q-many-text]').textContent = 'Yes, make all ' + q.count + ' videos.';
    var short = $('[data-q-short]'); short.hidden = !q.short;
    if(q.short && q.upgrade){ short.innerHTML = 'You need ' + credits(q.short) + ' more. <button type="button" class="linkbtn" data-upgrade-open>Upgrade your plan</button>'; if(window.cfyUpgrade) window.cfyUpgrade('That needs ' + credits(q.total) + ' and you have ' + credits(q.balance) + '. Pick a plan to keep going.'); }
    else if(q.short){ short.innerHTML = 'You need ' + credits(q.short) + ' more. ' + (q.pack ? 'The ' + esc(q.pack.name) + ' pack ($' + q.pack.usd + ', ' + credits(q.pack.credits) + ') covers it. ' : '') + '<a href="' + esc(B.urls.billing) + '">Get credits</a>'; }
    var ok = $('[data-confirm-make]'); ok.textContent = !n ? 'Nothing ticked' : (n === 1 ? 'Make it' : 'Make them') + ' · ' + credits(q.total);
    ok.disabled = !n || !!q.short || !!q.confirm; ok.dataset.total = q.total;
    $('[data-q-err]').hidden = true;
    if(d.open) return;                          // an ask's box re-prices in place as parts are ticked or edited
    open('price'); if(set) $$('[data-plan-text]', d).forEach(fit);
    (set ? $('[data-plan-on]', d) || ok : q.short ? $('[data-close]', d) : ok).focus();
  }
  $('[data-q-many]').addEventListener('change', function(e){ $('[data-confirm-make]').disabled = !e.target.checked || !!(lastQuote && lastQuote.short); });
  function qerr(msg){ var e = $('[data-q-err]'); e.hidden = !msg; e.textContent = msg || ''; }
  function generate(btn){
    // Only ever called from the price box's button, with the total that box showed.
    var body = payload(); body.total = +btn.dataset.total; body.confirm_many = $('[data-q-many]').checked;
    btn.disabled = true; var label = btn.textContent; btn.textContent = 'Starting';
    function back(){ btn.disabled = false; btn.textContent = label; }
    var first = B.claims_check ? post(B.urls.claims, payload()) : Promise.resolve({status: 'off'});
    first.then(function(c){
      if(c && c._status === 402){ back(); qerr(c.error); return null; }
      if(c && c.error && !c.status){ back(); qerr(c.error); return null; }
      if(c && c.status === 'flagged'){ back(); qerr('These lines say something the product page doesn\'t back up. Edit them and send again: ' + (c.flags || []).map(function(f){ return '“' + f.sentence + '”'; }).join(' ')); return null; }
      return post(B.urls.generate, body);
    }).then(function(r){
      if(!r) return;
      if(r.ok){ location.href = r.url; return; }
      back();
      if(r.card_required){ location.href = r.url; return; }
      if(r.verify && window.cfyVerify && window.CFY_VERIFY){ window.cfyVerify(r.reason).then(function(yes){ if(yes) generate(btn); else qerr(r.error); }); return; }
      if(r._status === 409 && asking() && asked){ requote(); qerr(r.error || 'The price changed. Check it and confirm again.'); return; }
      if(r._status === 409){ close(dlg('price')); say((r.error || 'The price changed.') + ' Press send to see it again.'); return; }
      qerr(r.error || 'It couldn\'t start. Try again.');
    }).catch(function(){ back(); qerr('Something went wrong. Try again.'); });
  }
  $('[data-confirm-make]').addEventListener('click', function(e){ if(!lastQuote) return; generate(e.currentTarget); });
})();
