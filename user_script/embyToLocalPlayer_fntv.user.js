// ==UserScript==
// @name         embyToLocalPlayer - 飞牛影视
// @namespace    https://github.com/kjtsune/embyToLocalPlayer
// @version      2026.06.08
// @description  XHR/fetch/pushState三重拦截 + 伪造成功响应防报错
// @match        *://*/v
// @match        *://*/v/
// @match        *://*/v/*
// @grant        unsafeWindow
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      127.0.0.1
// @run-at       document-start
// @license MIT
// ==/UserScript==

(function () {
'use strict';
var U = unsafeWindow, S = 'http://127.0.0.1:58000';
var ON = GM_getValue('fntvOn', true), BUSY = false;
var FAKE_RESP = '{"msg":"etlp","code":0,"data":{}}';

function L() { console.log.apply(console, ['[etlp]'].concat(Array.prototype.slice.call(arguments))); }
L('init');

function T() { var m = document.cookie.match(/Trim-MC-token=([^;]+)/); return m ? m[1] : ''; }

function G() {
    return new Promise(function (ok) {
        GM_xmlhttpRequest({
            method: 'POST', url: S + '/fnvideoL()',
            headers: { 'Content-Type': 'application/json' },
            onerror: function () { ok(null); },
        });
    });
}

function gm(url, method, body) {
    return new Promise(function (ok) {
        GM_xmlhttpRequest({
            method: method, url: url,
            data: body ? JSON.stringify(body) : null,
            headers: { Authorization: T(), 'Content-Type': 'application/json', Accept: 'application/json' },
            onload: function (r) { try { var j = JSON.parse(r.responseText); ok(j.code === 0 ? j.data : null); } catch (e) { ok(null); } },
            onerror: function () { ok(null); }, ontimeout: function () { ok(null); },
        });
    });
}

async function PLAY(guid) {
    if (BUSY || !guid) return;
    BUSY = true;
    var H = U.location.origin;

    var [streamData, playInfo, itemData] = await Promise.all([
        gm(H + '/v/api/v1/stream/list/' + guid, 'GET'),
        gm(H + '/v/api/v1/play/info', 'POST', { item_guid: guid }),
        gm(H + '/v/api/v1/item/' + guid, 'GET'),
    ]);

    if (!streamData || !streamData.files || !streamData.files.length) { BUSY = false; return; }
    var f = streamData.files[0];
    var v = (streamData.video_streams && streamData.video_streams[0]) || {};
    var a = (streamData.audio_streams && streamData.audio_streams[0]) || {};

    GM_xmlhttpRequest({
        method: 'POST', url: S + '/fnvideo/',
        data: JSON.stringify({
            server: 'fntv', host: H, token: T(),
            item_guid: guid, media_guid: f.guid,
            video_guid: v.guid || '',
            audio_guid: (playInfo && playInfo.audio_guid) || a.guid || '',
            subtitle_guid: (playInfo && playInfo.subtitle_guid) || '',
            title: (itemData && itemData.title) || '',
            original_title: (itemData && itemData.original_title) || '',
            tv_title: (itemData && itemData.tv_title) || '',
            parent_title: (itemData && itemData.parent_title) || '',
            type: (itemData && itemData.type) || '',
            season_number: (itemData && itemData.season_number) || 0,
            episode_number: (itemData && itemData.episode_number) || 0,
            parent_guid: (itemData && itemData.parent_guid) || '',
            file_path: f.path, file_name: f.file_name, file_size: f.size,
            start_sec: (playInfo && playInfo.ts) || (itemData && itemData.watched_ts) || 0,
            total_sec: (itemData && itemData.duration) || v.duration || 0,
            resolution: v.resolution_type || '',
            codec_name: v.codec_name || '', bitrate: v.bps || 0,
            subtitle_streams: streamData.subtitle_streams || [],
            audio_streams: streamData.audio_streams || [],
            video_streams: streamData.video_streams || [],
            mountDiskEnable: 'false',
        }),
        headers: { 'Content-Type': 'application/json' },
    });
    L('OK');
    setTimeout(function () { BUSY = false; }, 5000);
}

// ==== XHR prototype override ====
var XP = U.XMLHttpRequest.prototype;
var _open = XP.open, _send = XP.send;
var episodeGuid = null;

XP.open = function (method, url) {
    this.__u = url;
    return _open.apply(this, arguments);
};

function fakeXhrSuccess(self) {
    try { Object.defineProperty(self, 'readyState', { value: 4, configurable: true, writable: true }); } catch (e) {}
    try { Object.defineProperty(self, 'status', { value: 200, configurable: true, writable: true }); } catch (e) {}
    try { Object.defineProperty(self, 'statusText', { value: 'OK', configurable: true, writable: true }); } catch (e) {}
    try { Object.defineProperty(self, 'responseText', { value: FAKE_RESP, configurable: true, writable: true }); } catch (e) {}
    try { Object.defineProperty(self, 'response', { value: FAKE_RESP, configurable: true, writable: true }); } catch (e) {}
    try { self.onreadystatechange && self.onreadystatechange(); } catch (e) {}
    try { self.onload && self.onload(); } catch (e) {}
}

XP.send = function (body) {
    var u = this.__u || '', self = this;

    // Intercept play/info response to extract episode guid
    if (u.indexOf('/play/info') >= 0) {
        var origReady = this.onreadystatechange;
        this.onreadystatechange = function () {
            if (self.readyState === 4 && self.status === 200) {
                try {
                    var r = JSON.parse(self.responseText);
                    if (r.code === 0 && r.data && r.data.guid) {
                        episodeGuid = r.data.guid;
                        L('epGuid:', episodeGuid);
                    }
                } catch (e) {}
            }
            if (origReady) origReady.apply(this, arguments);
        };
    }

    // Block play/play, stream (non-list), m3u8, ts
    var isPlay = u.indexOf('/play/play') >= 0 ||
                 (u.indexOf('/v/api/v1/stream') >= 0 && u.indexOf('stream/list') < 0 && u.indexOf('streams') < 0);
    var isMedia = u.indexOf('.m3u8') >= 0 || u.indexOf('/media/') >= 0 || u.indexOf('.ts') >= 0;

    if (isPlay || isMedia) {
        L('block XHR:', u.replace(/.*\/v\/api\/v1\//, ''));
        var g = null;
        if (body && typeof body === 'string') {
            try { var b = JSON.parse(body); g = b.item_guid || null; } catch (e) {}
        }
        if (!g) {
            var ps = U.location.pathname.split('/').filter(function (x) { return x; });
            if (ps[0] === 'v' && ps[1] === 'video') g = ps[2];
            else if (ps[0] === 'v' && ps[1] === 'tv' && ps[2] === 'episode') g = ps[3];
            else if (ps[0] === 'v' && ps[1] === 'movie') g = ps[2];
        }
        if (!g) g = episodeGuid;
        if (g) PLAY(g);
        setTimeout(function () { fakeXhrSuccess(self); }, 10);
        return;
    }
    return _send.apply(this, arguments);
};

// ==== History ====
var _ps = U.history.pushState, _rs = U.history.replaceState;
U.history.pushState = function (s, t, u) {
    if (u && typeof u === 'string' && u.indexOf('/v/video/') >= 0) {
        var g = u.split('/').filter(function (x) { return x; }).pop();
        L('pushState block:', g);
        PLAY(g);
        return;
    }
    return _ps.apply(this, arguments);
};
U.history.replaceState = function (s, t, u) {
    if (u && typeof u === 'string' && u.indexOf('/v/video/') >= 0) {
        var g = u.split('/').filter(function (x) { return x; }).pop();
        L('replaceState block:', g);
        PLAY(g);
        return;
    }
    return _rs.apply(this, arguments);
};

// ==== Fetch ====
var _fetch = U.fetch;
U.fetch = function (input, options) {
    var url = typeof input === 'string' ? input : (input && input.url) || '';

    if (url.indexOf('/play/info') >= 0) {
        return _fetch(input, options).then(function (r) {
            var c = r.clone();
            c.json().then(function (j) {
                if (j.code === 0 && j.data && j.data.guid) {
                    episodeGuid = j.data.guid;
                    L('epGuid(fetch):', episodeGuid);
                }
            }).catch(function () {});
            return r;
        });
    }

    var isPlay = url.indexOf('/play/play') >= 0 ||
                 (url.indexOf('/stream') >= 0 && url.indexOf('stream/list') < 0 && url.indexOf('streams') < 0);
    var isMedia = url.indexOf('.m3u8') >= 0 || url.indexOf('/media/') >= 0;

    if (isPlay || isMedia) {
        L('block fetch:', url.replace(/.*\/v\/api\/v1\//, ''));
        PLAY(episodeGuid);
        return Promise.resolve(new Response(FAKE_RESP, {
            status: 200, statusText: 'OK', headers: { 'Content-Type': 'application/json' }
        }));
    }
    return _fetch(input, options);
};

GM_registerMenuCommand('FNTV: OFF', function () { ON = false; GM_setValue('fntvOn', false); L('OFF'); });
GM_registerMenuCommand('FNTV: ON',  function () { ON = true;  GM_setValue('fntvOn', true);  L('ON'); });
L('ready');
})();
