/* Local TikTok gift catalog. Never trust image URLs supplied by an event. */
(function (root) {
    'use strict';
    const normalize = value => typeof value === 'string' ? value.normalize('NFKC').trim().replace(/\s+/gu, ' ').toLowerCase() : '';
    const idKey = value => /^(?:[1-9]\d{0,19})$/.test(String(value)) ? String(value) : '';
    let entries = [];
    function resolve(msg, catalog = entries) {
        if (!msg || msg.platform !== 'tiktok' || msg.eventType !== 'gift') return null;
        const valid = catalog.filter(item => item && /^[A-Za-z0-9_. -]+\.png$/.test(item.asset) && !item.asset.includes('..'));
        const id = idKey(msg.giftId);
        const byId = id ? valid.filter(item => Array.isArray(item.verifiedIds) && item.verifiedIds.some(value => idKey(value) === id)) : [];
        if (byId.length) return byId.length === 1 ? byId[0] : null;
        const name = normalize(msg.giftName);
        const byName = name ? valid.filter(item => Array.isArray(item.names) && item.names.some(value => normalize(value) === name)) : [];
        return byName.length === 1 ? byName[0] : null;
    }
    const pending = new Set();
    function decorate(container, msg) {
        if (msg.eventType !== 'gift') return;
        const marker = document.createElement('span');
        marker.className = 'gift-art';
        marker.style.cssText = 'display:inline-flex;vertical-align:middle;align-items:center;margin-right:10px;flex-shrink:0';
        container.prepend(marker);
        function update() {
            const match = resolve(msg);
            marker.replaceChildren();
            if (!match) { marker.textContent = '🎁'; return; }
            const image = document.createElement('img');
            image.src = '/gifts/' + encodeURIComponent(match.asset);
            image.alt = String(msg.giftName || 'Gift');
            image.width = 64; image.height = 64;
            image.style.cssText = 'width:64px;height:64px;object-fit:contain';
            image.addEventListener('error', () => { marker.replaceChildren(); marker.textContent = '🎁'; }, { once: true });
            marker.append(image);
        }
        update();
        if (!loaded) pending.add({ marker, update });
    }
    function text(msg) {
        if (msg.eventType !== 'gift' || !msg.giftName) return msg.comment;
        const count = Number.isSafeInteger(msg.giftCount) && msg.giftCount > 0 ? msg.giftCount : 1;
        return String(msg.giftName) + ' ×' + count + (msg.giftValue ? ' · ' + msg.giftValue : '');
    }
    let loaded = false;
    const ready = typeof document !== 'undefined' ? fetch('/gifts/catalog.json').then(response => {
        if (!response.ok) throw new Error('Catalog unavailable');
        return response.json();
    }).then(data => { entries = Array.isArray(data.entries) ? data.entries : []; }).catch(() => {}).finally(() => {
        loaded = true;
        for (const item of pending) if (item.marker.isConnected) item.update();
        pending.clear();
    }) : Promise.resolve();
    root.GiftRenderer = { resolve, normalize, decorate, text, ready };
    if (typeof module !== 'undefined') module.exports = root.GiftRenderer;
})(typeof window !== 'undefined' ? window : globalThis);
