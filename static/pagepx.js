/* Conversion tracking on a member's Campaignify page (conversions.py, roadmap M3.4).
   The server decided this visit may be measured (the page's measurement setting, a US visitor, no GPC or
   Do Not Track) and wrote the config into #cfy-px: pixel ids and this render's event ids only. Each event
   fires with the same id the server sends, so every platform counts it once. Nothing personal is read here. */
(function () {
  var el = document.getElementById('cfy-px');
  if (!el) return;
  var C;
  try { C = JSON.parse(el.textContent || '{}'); } catch (e) { return; }
  if (navigator.globalPrivacyControl === true || navigator.doNotTrack === '1' || window.doNotTrack === '1') return;
  var nonce = el.nonce || el.getAttribute('nonce') || '';
  function load(src) {
    var s = document.createElement('script');
    s.async = true; s.src = src; if (nonce) s.nonce = nonce;
    document.head.appendChild(s);
  }
  // Meta Pixel: eventID pairs with the Conversions API's event_id.
  if (C.meta && C.meta.length) {
    if (!window.fbq) {
      var fq = window.fbq = function () { fq.callMethod ? fq.callMethod.apply(fq, arguments) : fq.queue.push(arguments); };
      window._fbq = window._fbq || fq; fq.push = fq; fq.loaded = true; fq.version = '2.0'; fq.queue = [];
      load('https://connect.facebook.net/en_US/fbevents.js');
    }
    for (var i = 0; i < C.meta.length; i++) { fbq('set', 'autoConfig', false, C.meta[i]); fbq('init', C.meta[i]); }
    fbq('track', 'PageView', {}, {eventID: C.pv});
    if (C.lead) fbq('track', 'Lead', {}, {eventID: C.lead});
  }
  // TikTok Pixel: event_id pairs with the Events API's event_id.
  if (C.tiktok && C.tiktok.length) {
    window.TiktokAnalyticsObject = 'ttq';
    var tq = window.ttq = window.ttq || [];
    tq.methods = ['page', 'track', 'identify', 'instances', 'debug', 'on', 'off', 'once', 'ready', 'alias', 'group',
                  'enableCookie', 'disableCookie', 'holdConsent', 'revokeConsent', 'grantConsent'];
    tq.setAndDefer = function (obj, m) { obj[m] = function () { obj.push([m].concat([].slice.call(arguments))); }; };
    for (var k = 0; k < tq.methods.length; k++) tq.setAndDefer(tq, tq.methods[k]);
    tq.instance = function (id) { var q = tq._i[id] || []; for (var j = 0; j < tq.methods.length; j++) tq.setAndDefer(q, tq.methods[j]); return q; };
    tq.load = function (id, opts) {
      var u = 'https://analytics.tiktok.com/i18n/pixel/events.js';
      tq._i = tq._i || {}; tq._i[id] = []; tq._i[id]._u = u; tq._t = tq._t || {}; tq._t[id] = +new Date;
      tq._o = tq._o || {}; tq._o[id] = opts || {};
      load(u + '?sdkid=' + encodeURIComponent(id) + '&lib=ttq');
    };
    for (var t = 0; t < C.tiktok.length; t++) ttq.load(C.tiktok[t]);
    ttq.page();
    ttq.track('ViewContent', {}, {event_id: C.pv});
    if (C.lead) ttq.track('SubmitForm', {}, {event_id: C.lead});
  }
})();
