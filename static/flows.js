/* Flow editor: a vertical step list (conditions split into Yes / No columns that
   rejoin), an inspector for the selected step, save, timeline and CSV export.
   The page is server-rendered; this script edits the flow's tree and saves it
   as JSON (the server cleans and checks it again). */
(function(){
  'use strict';
  var B = JSON.parse(document.getElementById('fl-boot').textContent);
  var CSRF = (document.querySelector('input[name=csrf]') || {}).value || '';
  var T = B.tree, probs = B.problems || {}, sel = 'trigger', dirty = false, saving = false;
  var $ = function(s, r){ return (r || document).querySelector(s); }, $$ = function(s, r){ return [].slice.call((r || document).querySelectorAll(s)); };
  var stack = $('[data-stack]'), insp = $('[data-insp]');
  function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function uid(){ return 'n' + Math.random().toString(16).slice(2, 8); }
  var ICON = {
    trigger: '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>', email: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M4 7l8 6 8-6"/>',
    sms: '<path d="M4 5h16v11H9l-5 4z"/>', wait: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    split: '<path d="M12 4v5M12 9L6 15v5M12 9l6 6v5"/>',
    condition: '<path d="M6 4v6a4 4 0 004 4h8M14 10l4 4-4 4"/><circle cx="6" cy="4" r="1.5"/>', exit_if: '<path d="M14 5h5v14h-5M10 8l-4 4 4 4M6 12h10"/>',
    tag: '<path d="M20.6 13.4l-7.2 7.2a2 2 0 01-2.8 0L3 13V3h10l7.6 7.6a2 2 0 010 2.8z"/><circle cx="7.5" cy="7.5" r="1.4"/>',
    stage: '<path d="M4 18h4v-4h4v-4h4V6h4"/>', task: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 12l3 3 5-6"/>',
    notify: '<path d="M6 16V11a6 6 0 0112 0v5l2 2H4zM10 20a2 2 0 004 0"/>', webhook: '<circle cx="7" cy="16" r="2.5"/><circle cx="17" cy="16" r="2.5"/><circle cx="12" cy="6" r="2.5"/><path d="M9.5 16h5M11 8.5L8 14M13 8.5l3 5.5"/>',
    community: '<path d="M4 5h16v10H9l-5 4z"/><path d="M8 9h8M8 12h5"/>', end: '<rect x="6" y="6" width="12" height="12" rx="2"/>'};
  function icon(t){ return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + (ICON[t] || '') + '</svg>'; }

  // ---- tree helpers
  function BR(s){ return s.type === 'condition' || s.type === 'split'; }
  function walk(list, fn){ list.forEach(function(s, i){ fn(s, list, i); if(BR(s)){ walk(s.yes, fn); walk(s.no, fn); } }); }
  function find(id){ var hit = null; walk(T.steps, function(s, list, i){ if(s.id === id) hit = {step: s, list: list, i: i}; }); return hit; }
  function emails(){ var out = []; walk(T.steps, function(s){ if(s.type === 'email') out.push(s); }); return out; }
  function defaults(type){
    var s = {id: uid(), type: type};
    if(type === 'email'){ s.subject = ''; s.body = 'Hi {{first_name}},\n\n\n\n{{brand}}'; }
    if(type === 'sms') s.body = '{{brand}}: ';
    if(type === 'wait'){ s.days = 1; s.hours = 0; s.at = ''; }
    if(type === 'condition'){ s.test = {kind: 'has_tag', tag: ''}; s.yes = []; s.no = []; }
    if(type === 'split'){ s.pct = 50; s.yes = []; s.no = []; }
    if(type === 'exit_if') s.test = {kind: 'has_tag', tag: ''};
    if(type === 'tag'){ s.op = 'add'; s.tag = ''; }
    if(type === 'stage') s.stage = B.stages[0];
    if(type === 'task'){ s.title = ''; s.body = ''; s.due_days = 0; }
    if(type === 'notify') s.message = '';
    if(type === 'webhook') s.url = 'https://';
    return s;
  }
  function plural(n, w){ return n + ' ' + w + (n === 1 ? '' : 's'); }
  function testText(t){
    var k = t.kind;
    if(k === 'has_tag') return 'they have the tag “' + (t.tag || '…') + '”';
    if(k === 'field_equals') return (t.field || 'field') + ' is “' + (t.value || '') + '”';
    if(k === 'submitted_form'){ var f = B.forms.filter(function(x){ return x.id == t.form_id; })[0]; return 'they filled in ' + (f ? f.name : 'a form'); }
    if(k === 'clicked' || k === 'opened'){ var e = find(t.node); return 'they ' + (k === 'clicked' ? 'clicked a link in ' : 'opened ') + (e ? '“' + (e.step.subject || 'the email') + '”' : 'an email'); }
    if(k === 'has_phone') return 'they gave a phone number';
    if(k === 'has_consent') return 'they agreed to ' + (t.channel === 'sms' ? 'texts' : 'emails');
    return k;
  }
  function summary(s){
    switch(s.type){
      case 'email': return s.subject ? '“' + s.subject + '”' : 'No subject yet';
      case 'sms': return (s.body || '').slice(0, 70) || 'No text yet';
      case 'wait': var b = []; if(+s.days) b.push(plural(+s.days, 'day')); if(+s.hours) b.push(plural(+s.hours, 'hour')); return (b.join(' ') || '') + (s.at ? (b.length ? ', then ' : '') + 'until ' + s.at : '') || 'Set a time';
      case 'condition': return 'If ' + testText(s.test);
      case 'split': return 'A: ' + s.pct + '% · B: ' + (100 - s.pct) + '% of people';
      case 'exit_if': return 'Leave the flow when ' + testText(s.test);
      case 'tag': return (s.op === 'remove' ? 'Remove ' : 'Add ') + '“' + (s.tag || '…') + '”';
      case 'stage': return 'Stage: ' + s.stage;
      case 'task': return s.title || 'No title yet';
      case 'notify': return s.message || 'No note yet';
      case 'webhook': return (s.url || '').replace(/^https:\/\//, '') || 'No address yet';
      case 'community': return 'Leaves you a task with the join link';
    }
    return '';
  }
  function trigText(){
    var t = T.trigger || {}, k = t.kind;
    if(!k) return 'Pick what starts it';
    if(k === 'form_submitted'){ var f = B.forms.filter(function(x){ return x.id == t.form_id; })[0]; return 'When ' + (f ? '“' + f.name + '”' : 'a form') + ' is submitted'; }
    if(k === 'tag_added') return 'When the tag “' + (t.tag || '…') + '” is added';
    if(k === 'date') return 'On ' + (t.at ? t.at.replace('T', ' at ') : '…') + ', everyone tagged “' + (t.tag || '…') + '”';
    return B.triggers[k];
  }

  // ---- render the step list
  var lists = [];
  function card(s){
    var p = probs[s.id] || [], typeName = (B.types[s.type] || {}).name || s.type;
    var chan = (s.type === 'email' && !B.channels.email) || (s.type === 'sms' && !B.channels.sms);
    return '<div class="fl-card t-' + s.type + (sel === s.id ? ' sel' : '') + (p.length ? ' bad' : '') + '" data-id="' + esc(s.id) + '" tabindex="0" role="button" aria-pressed="' + (sel === s.id) + '" aria-label="' + esc(typeName + ': ' + summary(s)) + '">' +
      '<span class="fl-ico">' + icon(s.type) + '</span><span class="fl-txt"><b>' + esc(typeName) + (chan ? ' <em class="lock">as a task</em>' : '') + '</b><span>' + esc(summary(s)) + '</span>' +
      (s.hint ? '<i>' + esc(s.hint) + '</i>' : '') + (p.length ? '<span class="fl-err">' + esc(p.join(' ')) + '</span>' : '') + '</span></div>';
  }
  function ins(list, i){ lists.push(list); return '<div class="fl-ins"><button type="button" class="fl-plus" data-ins="' + (lists.length - 1) + ':' + i + '" aria-label="Add a step here">+</button></div>'; }
  function seq(list){
    var h = ins(list, 0);
    list.forEach(function(s, i){
      h += card(s);
      if(BR(s)){
        var sp = s.type === 'split', la = sp ? 'A · ' + s.pct + '%' : 'Yes', lb = sp ? 'B · ' + (100 - s.pct) + '%' : 'No';
        h += '<div class="fl-branches"><div class="fl-br"><span class="fl-brl ' + (sp ? 'ab' : 'yes') + '">' + la + '</span>' + seq(s.yes) + '</div><div class="fl-br"><span class="fl-brl ' + (sp ? 'ab' : 'no') + '">' + lb + '</span>' + seq(s.no) + '</div></div><div class="fl-join" aria-hidden="true"></div>';
      }
      h += ins(list, i + 1);
    });
    return h;
  }
  function renderStack(){
    lists = [];
    var tp = probs.trigger || [];
    stack.innerHTML = '<div class="fl-card t-trigger' + (sel === 'trigger' ? ' sel' : '') + (tp.length ? ' bad' : '') + '" data-id="trigger" tabindex="0" role="button" aria-pressed="' + (sel === 'trigger') + '"><span class="fl-ico">' + icon('trigger') + '</span><span class="fl-txt"><b>Starts</b><span>' + esc(trigText()) + '</span>' + (tp.length ? '<span class="fl-err">' + esc(tp.join(' ')) + '</span>' : '') + '</span></div>' +
      seq(T.steps) + '<div class="fl-card t-end" aria-label="End"><span class="fl-ico">' + icon('end') + '</span><span class="fl-txt"><b>End</b><span>They leave the flow</span></span></div>' +
      ((probs.flow || []).length ? '<p class="flash bad">' + esc(probs.flow.join(' ')) + '</p>' : '');
  }

  // ---- inspector
  function opt(v, label, cur){ return '<option value="' + esc(v) + '"' + (String(cur) === String(v) ? ' selected' : '') + '>' + esc(label) + '</option>'; }
  function field(label, html, help){ return '<label>' + label + (help ? ' <span class="dim">' + help + '</span>' : '') + '</label>' + html; }
  function input(name, val, attrs){ return '<input type="text" data-f="' + name + '" value="' + esc(val) + '" ' + (attrs || '') + '>'; }
  function testFields(t){
    var h = field('Check', '<select data-f="test.kind">' + Object.keys(B.tests).map(function(k){ return opt(k, B.tests[k], t.kind); }).join('') + '</select>');
    if(t.kind === 'has_tag') h += field('Tag', input('test.tag', t.tag || '', 'list="fl-tags" maxlength="30"'));
    if(t.kind === 'field_equals') h += field('Field', '<select data-f="test.field">' + ['stage', 'name', 'email', 'phone', 'source'].map(function(k){ return opt(k, k, t.field); }).join('') + '</select>') + field('Equals', input('test.value', t.value || ''));
    if(t.kind === 'submitted_form') h += field('Form', '<select data-f="test.form_id">' + opt('', 'Pick a form', t.form_id) + B.forms.map(function(f){ return opt(f.id, f.name, t.form_id); }).join('') + '</select>');
    if(t.kind === 'clicked' || t.kind === 'opened'){ h += field('Email', '<select data-f="test.node">' + opt('', 'Pick an email', t.node) + emails().map(function(e){ return opt(e.id, e.subject || '(no subject)', t.node); }).join('') + '</select>');
      if(!B.channels.email) h += '<p class="small muted">Until email sending is connected, this is always “no”.</p>'; }
    if(t.kind === 'has_consent') h += field('Channel', '<select data-f="test.channel">' + opt('email', 'Email', t.channel) + opt('sms', 'Texts', t.channel) + '</select>');
    return h;
  }
  function preview(s){
    var c = {first_name: 'Jamie', name: 'Jamie Lee', product: B.brand.product || 'your product', brand: B.brand.name || 'Your brand'};
    function r(x){ return String(x || '').replace(/\{\{\s*(first_name|name|product|brand)\s*\}\}/g, function(m, k){ return c[k]; }).replace(/\{\{\s*link:([a-z0-9-]+)\s*\}\}/g, function(m, sl){ return 'campaignify.ai/p/' + sl; }); }
    if(s.type === 'email') return '<div class="fl-prev"><b>' + esc(r(s.subject) || '(no subject)') + '</b><p>' + esc(r(s.body)).replace(/\n/g, '<br>') + '</p><p class="small dim">Sent as “' + esc(c.brand) + ' via Campaignify”, with your postal address and a one-click unsubscribe link added at the bottom.</p></div>';
    var body = r(s.body);
    return '<div class="fl-prev sms"><p>' + esc(body) + '</p><p class="small dim">The first text to each person adds: Reply STOP to opt out, HELP for help. Msg &amp; data rates may apply.</p></div>';
  }
  function segs(body){
    var gsm = /^[A-Za-z0-9 \r\n@£$¥èéùìòÇØøÅå_ÆæßÉ!"#%&'()*+,\-./:;<=>?¡ÄÖÑÜ§¿äöñüà^{}\\\[~\]|€]*$/.test(body || '');
    var n = gsm ? (body || '').length : Array.from(body || '').length;
    var per = gsm ? [160, 153] : [70, 67];
    return {n: n, segs: n <= per[0] ? 1 : Math.ceil(n / per[1]), enc: gsm ? 'GSM-7' : 'Unicode'};
  }
  function renderInsp(){
    var h = '';
    if(sel === 'trigger'){
      var t = T.trigger || {};
      h = '<h3>What starts it</h3>' + field('Trigger', '<select data-f="trigger.kind">' + opt('', 'Pick one', t.kind) + Object.keys(B.triggers).map(function(k){ return opt(k, B.triggers[k], t.kind); }).join('') + '</select>');
      if(t.kind === 'form_submitted') h += field('Form', '<select data-f="trigger.form_id">' + opt('', B.forms.length ? 'Any of my forms' : 'No forms yet', t.form_id) + B.forms.map(function(f){ return opt(f.id, f.name, t.form_id); }).join('') + '</select>');
      if(t.kind === 'tag_added' || t.kind === 'date') h += field('Tag', input('trigger.tag', t.tag || '', 'list="fl-tags" maxlength="30"'), t.kind === 'date' ? '(everyone with it starts)' : '');
      if(t.kind === 'date') h += field('When', '<input type="datetime-local" data-f="trigger.at" value="' + esc((t.at || '').slice(0, 16)) + '">', '(your time zone)');
      h += '<p class="small muted">Each person goes through a flow once.</p>';
    } else {
      var hit = find(sel); if(!hit){ sel = 'trigger'; return renderInsp(); }
      var s = hit.step, name = (B.types[s.type] || {}).name;
      h = '<h3>' + esc(name) + '</h3>';
      if(s.hint) h += '<p class="small muted fl-hint">' + esc(s.hint) + '</p>';
      if(s.type === 'email'){
        h += field('Subject', input('subject', s.subject, 'maxlength="150"'), '(plain, no fake “Re:”)');
        h += field('Email', '<textarea data-f="body" rows="9" maxlength="5000">' + esc(s.body) + '</textarea>');
        h += '<div class="fl-merge small">Insert: ' + ['first_name', 'product', 'brand'].map(function(k){ return '<button type="button" class="linkbtn" data-merge="{{' + k + '}}">' + k.replace('_', ' ') + '</button>'; }).join(' · ') +
          (B.pages.length ? ' · <select data-mergelink aria-label="Insert a page link"><option value="">page link…</option>' + B.pages.map(function(p){ return '<option value="{{link:' + esc(p.slug) + '}}">' + esc(p.title || p.slug) + '</option>'; }).join('') + '</select>' : '') + '</div>';
        if(!B.channels.email) h += '<p class="small muted">Email sending isn’t connected yet: when someone reaches this step you get a task with this email, ready to send.</p>';
        h += preview(s);
      } else if(s.type === 'sms'){
        var g = segs(s.body);
        h += field('Text', '<textarea data-f="body" rows="4" maxlength="640">' + esc(s.body) + '</textarea>');
        h += '<p class="small ' + (g.segs > 1 ? 'warnc' : 'muted') + '" data-segs>' + g.n + ' characters · ' + plural(g.segs, 'segment') + ' (' + g.enc + ')' + (g.segs > 1 ? ': keep it to one if you can' : '') + '</p>';
        h += '<p class="small muted">Only sent to people who agreed to texts, between 9:00 and 20:00 their time.' + (B.channels.sms ? '' : ' Text sending isn’t connected yet: this becomes a task for you.') + '</p>';
        h += preview(s);
      } else if(s.type === 'wait'){
        h += '<div class="two-col">' + field('Days', '<input type="number" min="0" max="365" data-f="days" value="' + (+s.days || 0) + '">') + field('Hours', '<input type="number" min="0" max="23" data-f="hours" value="' + (+s.hours || 0) + '">') + '</div>';
        h += field('Then wait until', '<input type="time" data-f="at" value="' + esc(s.at || '') + '">', '(optional, their local time)');
      } else if(s.type === 'condition' || s.type === 'exit_if'){
        h += testFields(s.test);
        if(s.type === 'condition') h += '<p class="small muted">Yes and No each get their own steps; both carry on with whatever comes after.</p>';
      } else if(s.type === 'split'){
        h += field('Share of people on path A', '<input type="number" min="1" max="99" data-f="pct" value="' + (+s.pct || 50) + '">', '(%; the rest take B)');
        h += '<p class="small muted">Each person is assigned once, at random. Compare the two paths in Activity.</p>';
      } else if(s.type === 'tag'){
        h += field('Do', '<select data-f="op">' + opt('add', 'Add the tag', s.op) + opt('remove', 'Remove the tag', s.op) + '</select>') + field('Tag', input('tag', s.tag, 'list="fl-tags" maxlength="30"'));
        h += '<p class="small muted">Adding a tag can start other flows that begin with it.</p>';
      } else if(s.type === 'stage'){
        h += field('Stage', '<select data-f="stage">' + B.stages.map(function(k){ return opt(k, k, s.stage); }).join('') + '</select>');
      } else if(s.type === 'task'){
        h += field('Title', input('title', s.title, 'maxlength="140" placeholder="e.g. Call {{first_name}}"')) + field('Details', '<textarea data-f="body" rows="4" maxlength="2000">' + esc(s.body) + '</textarea>') + field('Due in', '<input type="number" min="0" max="60" data-f="due_days" value="' + (+s.due_days || 0) + '">', '(days)');
      } else if(s.type === 'notify'){
        h += field('Note to you', input('message', s.message, 'maxlength="300" placeholder="e.g. {{name}} clicked the offer"')) + '<p class="small muted">Shows up in your Tasks.</p>';
      } else if(s.type === 'webhook'){
        h += field('Send to', input('url', s.url, 'maxlength="500" inputmode="url"'), '(https only)') + '<p class="small muted">POSTs the contact and step as JSON, signed with this flow’s secret (X-Campaignify-Signature).</p>';
      } else if(s.type === 'community'){
        h += '<p class="small muted">Leaves you a task with your community’s join link to send them. Needs an open join link in Community.</p>';
      }
      h += '<div class="fl-move"><button type="button" class="btn dark sm" data-mv="-1" ' + (hit.i === 0 ? 'disabled' : '') + '>Move up</button><button type="button" class="btn dark sm" data-mv="1" ' + (hit.i === hit.list.length - 1 ? 'disabled' : '') + '>Move down</button><button type="button" class="btn dark sm" data-dup>Duplicate</button><button type="button" class="linkbtn" data-del style="color:var(--bad)">Delete step</button></div>';
      var p = probs[s.id] || [];
      if(p.length) h = '<div class="flash bad">' + esc(p.join(' ')) + '</div>' + h;
    }
    insp.innerHTML = h + '<datalist id="fl-tags">' + B.tags.map(function(t){ return '<option value="' + esc(t) + '">'; }).join('') + '</datalist>';
  }
  function render(){ renderStack(); renderInsp(); readiness(); }
  function setDirty(on){ dirty = on; $('[data-saved]').textContent = on ? 'Unsaved changes' : 'All changes saved'; $('[data-saved]').classList.toggle('warnc', on);
    var u = $('[data-unsaved-note]'); if(u) u.hidden = !on; var to = $('[data-turnon]'); if(to) to.disabled = on || hasProblems(); var pb = $('[data-publish]'); if(pb) pb.disabled = on; }
  function hasProblems(){ return Object.keys(probs).length > 0; }
  function readiness(){
    $('[data-ready]').innerHTML = (B.readiness || []).map(function(r){ return '<li class="' + (r.ok ? 'ok' : (r.block ? 'no' : 'note')) + '"><b>' + esc(r.label) + '</b>' + (r.detail ? '<span class="small muted">' + esc(r.detail) + '</span>' : '') + '</li>'; }).join('');
    var to = $('[data-turnon]'); if(to) to.disabled = dirty || hasProblems();
  }

  // ---- edits
  function setPath(obj, path, val){ var p = path.split('.'); while(p.length > 1){ var k = p.shift(); obj[k] = obj[k] || {}; obj = obj[k]; } obj[p[0]] = val; }
  function target(){ return sel === 'trigger' ? T : find(sel).step; }
  insp.addEventListener('input', function(e){
    var el = e.target, f = el.dataset.f; if(!f) return;
    var val = el.type === 'number' ? (+el.value || 0) : el.value;
    if(/form_id$/.test(f)) val = el.value ? +el.value : null;
    if(f === 'trigger.at') val = el.value;
    setPath(target(), f, val);
    setDirty(true); renderStack();
    if(f === 'body' && sel !== 'trigger' && find(sel).step.type === 'sms'){ var g = segs(val), sg = $('[data-segs]', insp); if(sg){ sg.textContent = g.n + ' characters · ' + plural(g.segs, 'segment') + ' (' + g.enc + ')' + (g.segs > 1 ? ': keep it to one if you can' : ''); sg.classList.toggle('warnc', g.segs > 1); } }
    var pv = $('.fl-prev', insp); if(pv && sel !== 'trigger'){ var tmp = document.createElement('div'); tmp.innerHTML = preview(find(sel).step); pv.replaceWith(tmp.firstChild); }
  });
  insp.addEventListener('change', function(e){
    var f = e.target.dataset.f;
    if(f === 'test.kind' || f === 'trigger.kind'){ if(f === 'test.kind'){ var st = target(); st.test = {kind: e.target.value}; } renderInsp(); var x = $('[data-f="' + f + '"]', insp); if(x) x.focus(); }
    if(e.target.matches('[data-mergelink]') && e.target.value){ insertAtBody(e.target.value); e.target.value = ''; }
  });
  function insertAtBody(text){ var ta = $('textarea[data-f=body]', insp); if(!ta) return; var a = ta.selectionStart || ta.value.length; ta.value = ta.value.slice(0, a) + text + ta.value.slice(ta.selectionEnd || a); ta.dispatchEvent(new Event('input', {bubbles: true})); ta.focus(); ta.selectionStart = ta.selectionEnd = a + text.length; }
  insp.addEventListener('click', function(e){
    var t = e.target;
    if(t.closest('[data-merge]')){ insertAtBody(t.closest('[data-merge]').dataset.merge); return; }
    var hit = sel !== 'trigger' && find(sel); if(!hit) return;
    if(t.closest('[data-mv]')){ var d = +t.closest('[data-mv]').dataset.mv, j = hit.i + d; if(j < 0 || j >= hit.list.length) return; hit.list.splice(hit.i, 1); hit.list.splice(j, 0, hit.step); setDirty(true); render(); return; }
    if(t.closest('[data-dup]')){ var copy = JSON.parse(JSON.stringify(hit.step)); (function re(s){ s.id = uid(); if(BR(s)){ s.yes.forEach(re); s.no.forEach(re); } })(copy); hit.list.splice(hit.i + 1, 0, copy); sel = copy.id; setDirty(true); render(); return; }
    if(t.closest('[data-del]')){ var s = hit.step, label = (B.types[s.type] || {}).name;
      var go = function(){ hit.list.splice(hit.i, 1); sel = 'trigger'; setDirty(true); render(); };
      if(BR(s) && (s.yes.length || s.no.length) && window.cfyConfirm) window.cfyConfirm('Delete this ' + label + ' and the steps in both branches?', 'Delete').then(function(ok){ if(ok) go(); }); else go(); }
  });

  // ---- selecting and adding
  var menu = null;
  function closeMenu(){ if(menu){ menu.remove(); menu = null; } }
  stack.addEventListener('click', function(e){
    var plus = e.target.closest('[data-ins]');
    if(plus){ e.stopPropagation(); closeMenu(); var parts = plus.dataset.ins.split(':'), list = lists[+parts[0]], at = +parts[1];
      menu = document.createElement('div'); menu.className = 'fl-menu'; menu.setAttribute('role', 'menu');
      var groups = {}; Object.keys(B.types).forEach(function(k){ var g = B.types[k].group; (groups[g] = groups[g] || []).push(k); });
      menu.innerHTML = Object.keys(groups).map(function(g){ return '<p>' + g + '</p>' + groups[g].map(function(k){ return '<button type="button" role="menuitem" data-add="' + k + '"><span class="fl-ico">' + icon(k) + '</span>' + esc(B.types[k].name) + '</button>'; }).join(''); }).join('');
      plus.parentNode.appendChild(menu); var first = menu.querySelector('button'); if(first) first.focus();
      menu.addEventListener('click', function(ev){ var b = ev.target.closest('[data-add]'); if(!b) return; var s = defaults(b.dataset.add); list.splice(at, 0, s); sel = s.id; closeMenu(); setDirty(true); render(); var c = stack.querySelector('[data-id="' + s.id + '"]'); if(c) c.scrollIntoView({block: 'nearest'}); focusInsp(); });
      menu.addEventListener('keydown', function(ev){ if(ev.key === 'Escape'){ closeMenu(); plus.focus(); } var bs = $$('button', menu), i = bs.indexOf(document.activeElement); if(ev.key === 'ArrowDown'){ ev.preventDefault(); (bs[i + 1] || bs[0]).focus(); } if(ev.key === 'ArrowUp'){ ev.preventDefault(); (bs[i - 1] || bs[bs.length - 1]).focus(); } });
      return; }
    var c = e.target.closest('.fl-card[data-id]'); if(c){ sel = c.dataset.id; renderStack(); renderInsp(); focusInsp(); }
  });
  stack.addEventListener('keydown', function(e){ var c = e.target.closest('.fl-card[data-id]'); if(c && (e.key === 'Enter' || e.key === ' ')){ e.preventDefault(); c.click(); } });
  document.addEventListener('click', function(e){ if(menu && !e.target.closest('.fl-menu')) closeMenu(); });
  function focusInsp(){ if(window.innerWidth <= 1000) insp.scrollIntoView({behavior: 'smooth', block: 'start'}); var f = insp.querySelector('input, textarea, select'); if(f) setTimeout(function(){ f.focus({preventScroll: window.innerWidth > 1000}); }, 60); }

  // ---- save
  $('[data-name]').addEventListener('input', function(){ setDirty(true); });
  function save(){
    if(saving) return Promise.resolve(); saving = true; $('[data-saved]').textContent = 'Saving…';
    return fetch(B.urls.save, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF': CSRF, 'Accept': 'application/json'},
      body: JSON.stringify({name: $('[data-name]').value, tree: T})}).then(function(r){ return r.json(); }).then(function(r){
      saving = false;
      if(r.error){ $('[data-saved]').textContent = r.error; return; }
      T = r.tree; probs = r.problems || {}; B.timeline = r.timeline; B.readiness = r.readiness; B.flow = r.flow;
      if(!find(sel) && sel !== 'trigger') sel = 'trigger';
      setDirty(false); render(); timeline();
      var pb = $('[data-publish]'); if(pb) pb.hidden = !r.flow.dirty;
      var n = Object.keys(probs).length; if(n) $('[data-saved]').textContent = 'Saved · ' + n + ' step' + (n > 1 ? 's' : '') + ' to fix';
    }).catch(function(){ saving = false; $('[data-saved]').textContent = 'Not saved: check your connection'; });
  }
  $('[data-save]').addEventListener('click', save);
  document.addEventListener('keydown', function(e){ if((e.ctrlKey || e.metaKey) && e.key === 's'){ e.preventDefault(); save(); } });
  window.addEventListener('beforeunload', function(e){ if(dirty){ e.preventDefault(); e.returnValue = ''; } });
  $$('.fl-onbtns button, form[action$="/state"] button').forEach(function(b){ b.addEventListener('click', function(e){ if(dirty){ e.preventDefault(); save(); } }); });

  // ---- tabs, timeline, export
  $$('[data-tab]').forEach(function(a){ a.addEventListener('click', function(e){ e.preventDefault(); var k = a.dataset.tab;
    $$('[data-tab]').forEach(function(x){ x.classList.toggle('on', x === a); x.setAttribute('aria-selected', x === a); });
    $$('[data-panel]').forEach(function(p){ p.hidden = p.dataset.panel !== k; }); try{ history.replaceState(null, '', '?tab=' + k); }catch(err){} }); });
  function when(r){ return (r.approx ? 'about ' : '') + 'Day ' + r.day + (r.hour ? ', +' + r.hour + ' h' : ''); }
  function timeline(){
    var rows = B.timeline || [];
    $('[data-timeline]').innerHTML = rows.length ? rows.map(function(r){ return '<tr><td>' + esc(when(r)) + '</td><td><span class="fl-ico sm">' + icon(r.type) + '</span>' + esc(r.label) + (r.hint ? '<div class="small muted">' + esc(r.hint) + '</div>' : '') + '</td><td class="small muted">' + esc(r.branch || 'everyone') + '</td></tr>'; }).join('') : '<tr><td colspan="3" class="muted">No steps yet.</td></tr>';
  }
  $('[data-print]').addEventListener('click', function(){ window.print(); });
  $('[data-csv]').addEventListener('click', function(){
    var q = function(s){ return '"' + String(s).replace(/"/g, '""') + '"'; };
    var csv = 'When,What,Path,Why\n' + (B.timeline || []).map(function(r){ return [when(r), r.label, r.branch || 'everyone', r.hint || ''].map(q).join(','); }).join('\n');
    var a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'})); a.download = ($('[data-name]').value || 'flow').replace(/[^a-z0-9]+/gi, '-').toLowerCase() + '-checklist.csv'; a.click();
  });

  render(); timeline(); setDirty(false);
})();
