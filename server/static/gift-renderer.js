/* Event-supplied TikTok CDN images. Local catalog is retained as inactive archive. */
(function (root) {
    'use strict';
    function allowedURL(value) {
        if (typeof value !== 'string' || !value || value.length > 2048 || /[\s\\]/u.test(value) || /[\x00-\x1f\x7f]/u.test(value)) return null;
        try {
            // Check the raw authority too: URL normalizes explicit default ports away.
            const authority = /^https:\/\/([^/?#]+)/i.exec(value);
            if (!authority || /[:@]/.test(authority[1])) return null;
            const url = new URL(value);
            const host = url.hostname.toLowerCase();
            return url.protocol === 'https:' && !url.username && !url.password && !url.port &&
                (host === 'tiktokcdn.com' || host.endsWith('.tiktokcdn.com')) ? value : null;
        } catch (_) { return null; }
    }
    function resolve(msg) {
        return msg && msg.platform === 'tiktok' && msg.eventType === 'gift' ? allowedURL(msg.giftImageUrl) : null;
    }
    function decorate(container, msg) {
        if (msg.eventType !== 'gift') return;
        const marker = document.createElement('span');
        marker.className = 'gift-art';
        marker.style.cssText = 'display:inline-flex;vertical-align:middle;align-items:center;margin-right:10px;flex-shrink:0';
        container.prepend(marker);
        const source = resolve(msg);
        if (!source) { marker.textContent = '🎁'; return; }
        const image = document.createElement('img');
        image.src = source;
        image.alt = String(msg.giftName || 'Gift');
        image.width = 64; image.height = 64;
        image.referrerPolicy = 'no-referrer';
        image.style.cssText = 'width:64px;height:64px;object-fit:contain';
        image.addEventListener('error', () => { marker.replaceChildren(); marker.textContent = '🎁'; }, { once: true });
        marker.append(image);
    }
    function text(msg) {
        if (msg.eventType !== 'gift' || !msg.giftName) return msg.comment;
        const count = Number.isSafeInteger(msg.giftCount) && msg.giftCount > 0 ? msg.giftCount : 1;
        return String(msg.giftName) + ' ×' + count + (msg.giftValue ? ' · ' + msg.giftValue : '');
    }
    root.GiftRenderer = { resolve, allowedURL, decorate, text };
    if (typeof module !== 'undefined') module.exports = root.GiftRenderer;
})(typeof window !== 'undefined' ? window : globalThis);
