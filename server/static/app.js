'use strict';

function readSecurityContext() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const fragmentToken = fragment.get('token');
    const fragmentSession = fragment.get('session');
    if (fragmentToken) sessionStorage.setItem('lca.token', fragmentToken);
    if (fragmentSession) sessionStorage.setItem('lca.session', fragmentSession);
    if (window.location.hash) history.replaceState(null, '', window.location.pathname + window.location.search);

    let sessionId = sessionStorage.getItem('lca.session');
    if (!sessionId) {
        sessionId = crypto.randomUUID().replaceAll('-', '');
        sessionStorage.setItem('lca.session', sessionId);
    }
    return { token: sessionStorage.getItem('lca.token') || '', sessionId: sessionId };
}

const security = readSecurityContext();
const socket = io({ auth: { token: security.token, sessionId: security.sessionId } });
const chatContainer = document.getElementById('chat-container');
const activeStreamsList = document.getElementById('active-streams-list');
const emptyStreams = document.getElementById('empty-streams');
const msgCounter = document.getElementById('msg-counter');
const toastContainer = document.getElementById('toast-container');
const streams = new Map();
let messageCount = 0;
let feedFilter = 'all';
const tts = window.speechSynthesis;
let thaiVoice = null;
let ttsRate = Number(localStorage.getItem('lca.tts.rate') || '1');

function chooseThaiVoice() {
    const voices = tts ? tts.getVoices() : [];
    thaiVoice = voices.find((voice) => /^th(-|_)/i.test(voice.lang))
        || voices.find((voice) => /thai|ไทย/i.test(voice.name))
        || null;
}
function speakComment(msg) {
    if (!tts) { showToast('เครื่องนี้ไม่รองรับระบบอ่านเสียง', 'error'); return; }
    const text = String(msg.comment || '').trim();
    if (!text) return;
    chooseThaiVoice();
    if (!thaiVoice) {
        showToast('ยังไม่มีเสียงภาษาไทยใน Windows — ติดตั้ง Thai voice แล้วเปิดโปรแกรมใหม่', 'error');
        return;
    }
    tts.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = thaiVoice.lang;
    utterance.voice = thaiVoice;
    utterance.rate = ttsRate;
    utterance.pitch = 1;
    utterance.volume = 1;
    utterance.onstart = () => showToast('กำลังอ่านความคิดเห็น', 'info');
    utterance.onerror = () => showToast('อ่านเสียงไม่สำเร็จ ตรวจสอบเสียงภาษาไทยใน Windows', 'error');
    tts.speak(utterance);
}
if (tts) {
    chooseThaiVoice();
    tts.addEventListener('voiceschanged', chooseThaiVoice);
}
document.getElementById('tts-test').addEventListener('click', () => {
    speakComment({ comment: 'นี่คือตัวอย่างเสียงอ่านความคิดเห็นภาษาไทย' });
});
document.getElementById('tts-stop').addEventListener('click', () => { if (tts) tts.cancel(); });
const feedContainer = document.getElementById('chat-container');
const feedZoomValue = document.getElementById('feed-zoom-value');
let feedScale = Number(localStorage.getItem('lca.feed.scale') || '1');
function refreshFeedScale() {
    feedScale = Math.max(0.5, Math.min(3, Math.round(feedScale * 10) / 10));
    feedContainer.style.setProperty('--feed-scale', String(feedScale));
    feedZoomValue.textContent = feedScale.toFixed(1) + 'x';
    localStorage.setItem('lca.feed.scale', String(feedScale));
}
document.getElementById('feed-zoom-out').addEventListener('click', () => { feedScale -= 0.5; refreshFeedScale(); });
document.getElementById('feed-zoom-in').addEventListener('click', () => { feedScale += 0.5; refreshFeedScale(); });
document.getElementById('feed-zoom-reset').addEventListener('click', () => { feedScale = 1; refreshFeedScale(); });
refreshFeedScale();
const filterButtons = { all: document.getElementById('feed-filter-all'), gifts: document.getElementById('feed-filter-gifts') };
function setFeedFilter(filter) {
    feedFilter = filter;
    Object.entries(filterButtons).forEach(([key, button]) => {
        const active = key === filter;
        button.className = 'text-xs px-3 py-1.5 rounded-lg border ' + (active ? 'bg-accent/10 text-accent border-accent/20' : 'bg-white/5 text-gray-400 border-white/10');
    });
    document.querySelectorAll('#chat-container .chat-item').forEach((item) => {
        item.style.display = filter === 'gifts' && item.dataset.eventType !== 'gift' ? 'none' : '';
    });
}
filterButtons.all.addEventListener('click', () => setFeedFilter('all'));
filterButtons.gifts.addEventListener('click', () => setFeedFilter('gifts'));

const contextMenu = makeElement('div', 'fixed hidden z-[100] min-w-40 rounded-lg border border-white/10 bg-[#18181b] p-1 shadow-2xl');
document.body.appendChild(contextMenu);

function hideContextMenu() { contextMenu.classList.add('hidden'); }
function showContextMenu(event, msg) {
    event.preventDefault();
    contextMenu.replaceChildren();
    const copy = makeElement('button', 'block w-full rounded px-3 py-2 text-left text-xs text-gray-200 hover:bg-accent/20 hover:text-white', 'Copy Comment');
    copy.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(String(msg.comment || ''));
            showToast('Comment copied', 'success');
        } catch (_error) { showToast('Copy failed. Please select the text manually.', 'error'); }
        hideContextMenu();
    });
    const paste = makeElement('button', 'block w-full rounded px-3 py-2 text-left text-xs text-gray-200 hover:bg-accent/20 hover:text-white', 'Paste to connection field');
    paste.addEventListener('click', async () => {
        try {
            const text = await navigator.clipboard.readText();
            document.getElementById('channel-id').value = text;
            document.getElementById('channel-id').focus();
            showToast('Pasted to connection field', 'success');
        } catch (_error) { showToast('Paste is blocked by the browser. Use Ctrl+V in the field.', 'error'); }
        hideContextMenu();
    });
    contextMenu.append(copy, paste);
    contextMenu.style.left = Math.min(event.clientX, window.innerWidth - 180) + 'px';
    contextMenu.style.top = Math.min(event.clientY, window.innerHeight - 90) + 'px';
    contextMenu.classList.remove('hidden');
}
document.addEventListener('click', hideContextMenu);
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') hideContextMenu(); });

// OBS Browser Source needs a persistent URL. Keep auth in the query string
// so copying the URL from the opened overlay does not lose the session token.
const overlayQuery = '?token=' + encodeURIComponent(security.token) + '&session=' + encodeURIComponent(security.sessionId);
const overlayProfiles = [
    { key: 'chat', link: 'chat-overlay-link', scale: 'chat-overlay-scale', path: 'overlay.html', extra: {} },
    { key: 'highlight', link: 'highlight-overlay-link', scale: 'highlight-overlay-scale', path: 'overlay_highlight.html', extra: {} },
    { key: 'vertical', link: 'highlight-vertical-link', scale: 'highlight-vertical-scale', path: 'overlay_highlight.html', extra: { size: 'xl', pos: 'top' } }
];
function overlayUrl(profile) {
    const params = new URLSearchParams({ ...profile.extra, overlay: profile.key, token: security.token, session: security.sessionId, scale: profile.value });
    return profile.path + '?' + params.toString();
}
overlayProfiles.forEach((profile) => {
    const selector = document.getElementById(profile.scale);
    const link = document.getElementById(profile.link);
    profile.value = Number(localStorage.getItem('lca.overlay.scale.' + profile.key) || '1');
    const refresh = () => {
        profile.value = Math.max(0.5, Math.min(3, Math.round(profile.value * 10) / 10));
        selector.value = profile.value.toFixed(1) + 'x';
        localStorage.setItem('lca.overlay.scale.' + profile.key, String(profile.value));
        link.href = overlayUrl(profile);
        socket.emit('overlay_scale', { overlay: profile.key, scale: profile.value, token: security.token, sessionId: security.sessionId });
    };
    document.querySelectorAll('[data-zoom="' + profile.key + '"]').forEach((button) => {
        button.addEventListener('click', () => { profile.value += Number(button.dataset.step) * 0.5; refresh(); });
    });
    document.querySelector('[data-reset-zoom="' + profile.key + '"]').addEventListener('click', () => {
        profile.value = 1;
        refresh();
    });
    refresh();
});

function authenticatedHeaders() {
    return {
        'Content-Type': 'application/json',
        'X-LCA-Token': security.token,
        'X-LCA-Session': security.sessionId
    };
}

async function fetchJson(path, options) {
    const response = await fetch(path, options);
    const payload = await response.json().catch(() => ({ status: 'error', message: 'Invalid server response' }));
    if (!response.ok) throw new Error(payload.message || 'Request failed');
    return payload;
}

const testEventButtons = [document.getElementById('test-youtube-event'), document.getElementById('test-tiktok-gift')];
const testEventStatus = document.getElementById('test-event-status');
async function sendTestEvent(platform) {
    testEventButtons.forEach(button => { button.disabled = true; button.setAttribute('aria-busy', 'true'); });
    testEventStatus.textContent = 'Sending simulated ' + platform + ' event…';
    try {
        await fetchJson('/api/events/test', { method: 'POST', headers: authenticatedHeaders(), body: JSON.stringify({ platform }) });
        testEventStatus.textContent = 'Simulated ' + platform + ' event sent. No live connection or payment.';
        showToast(testEventStatus.textContent, 'success');
    } catch (error) {
        testEventStatus.textContent = 'Test event failed: ' + error.message;
        showToast(testEventStatus.textContent, 'error');
    } finally {
        testEventButtons.forEach(button => { button.disabled = false; button.removeAttribute('aria-busy'); });
    }
}
testEventButtons[0].addEventListener('click', () => sendTestEvent('youtube'));
testEventButtons[1].addEventListener('click', () => sendTestEvent('tiktok'));

function showToast(message, type) {
    const toast = document.createElement('div');
    const colors = type === 'success' ? 'bg-green-500/10 border-green-500/20 text-green-400'
        : type === 'error' ? 'bg-red-500/10 border-red-500/20 text-red-400'
        : 'bg-accent/10 border-accent/20 text-accent';
    toast.className = 'toast text-xs px-4 py-2.5 rounded-lg border backdrop-blur-sm ' + colors;
    toast.textContent = String(message);
    toastContainer.appendChild(toast);
    setTimeout(() => { if (toast.parentNode) toast.remove(); }, 3000);
}

function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
}

const youtubeEmojiNames = {
    face_with_tears_of_joy: '😂', rolling_on_the_floor_laughing: '🤣', drooling_face: '🤤',
    joy: '😂', laughing: '😆', smiling_face_with_heart_eyes: '😍', heart_eyes: '😍',
    winking_face: '😉', blush: '😊', thinking_face: '🤔', pleading_face: '🥺', sob: '😭',
    fire: '🔥', red_heart: '❤️', heart: '❤️', thumbs_up: '👍', thumbs_down: '👎',
    clap: '👏', pray: '🙏', waving_hand: '👋', skull: '💀', poop: '💩',
    party_popper: '🎉', hundred_points: '💯', eyes: '👀', sparkles: '✨'
};
function renderComment(text) {
    return String(text || '').replace(/:([a-z0-9_+-]+):/gi, (match, name) => youtubeEmojiNames[name.toLowerCase()] || match);
}

async function addStream() {
    if (!security.token) {
        showToast('Missing local access token. Restart the Connector.', 'error');
        return;
    }
    const platform = document.getElementById('platform-select').value;
    const channelId = document.getElementById('channel-id').value.trim();
    if (!channelId) {
        showToast('Please enter a video ID, URL, or TikTok username', 'error');
        return;
    }
    try {
        const data = await fetchJson('/api/streams', {
            method: 'POST', headers: authenticatedHeaders(), body: JSON.stringify({ platform, channelId })
        });
        streams.set(data.stream.streamId, data.stream);
        renderStreams();
        document.getElementById('channel-id').value = '';
        showToast('Connection started. Waiting for the platform…', 'info');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function removeStream(streamId) {
    const stream = streams.get(streamId);
    if (!stream) return;
    try {
        await fetchJson('/api/streams/disconnect', {
            method: 'POST', headers: authenticatedHeaders(),
            body: JSON.stringify({ platform: stream.platform, channelId: stream.channelId })
        });
        streams.delete(streamId);
        renderStreams();
        showToast('Disconnected from ' + stream.channelId, 'info');
    } catch (error) {
        showToast(error.message, 'error');
    }
}

function renderStreams() {
    activeStreamsList.replaceChildren();
    if (streams.size === 0) {
        emptyStreams.style.display = 'block';
        activeStreamsList.appendChild(emptyStreams);
        return;
    }
    emptyStreams.style.display = 'none';
    streams.forEach((stream) => {
        const item = makeElement('div', 'bg-dark border border-white/5 p-3 rounded-lg flex items-center justify-between');
        const identity = makeElement('div', 'flex items-center gap-2');
        const dotColor = stream.state === 'failed' ? 'bg-red-500' : stream.state === 'connected' ? 'bg-green-500' : 'bg-yellow-500';
        identity.appendChild(makeElement('div', 'w-2 h-2 rounded-full ' + dotColor));
        identity.appendChild(makeElement('span', 'text-sm font-medium text-gray-200', stream.channelId));
        identity.appendChild(makeElement('span', 'text-[10px] uppercase text-gray-500', stream.state));
        const button = makeElement('button', 'text-xs bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500 hover:text-white px-2.5 py-1 rounded transition-all', 'Disconnect');
        button.addEventListener('click', () => removeStream(stream.streamId));
        item.append(identity, button);
        activeStreamsList.appendChild(item);
    });
}

function safeAvatar(value, author) {
    try {
        const parsed = new URL(value || '');
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href;
    } catch (_error) { /* use generated fallback */ }
    return 'https://ui-avatars.com/api/?name=' + encodeURIComponent(author) + '&background=random';
}

socket.on('connect_error', () => showToast('Local session authentication failed. Restart the Connector.', 'error'));
socket.on('stream_status', (stream) => {
    if (stream.state === 'disconnected') {
        streams.delete(stream.streamId);
        renderStreams();
        return;
    }
    streams.set(stream.streamId, stream);
    renderStreams();
    if (stream.state === 'connected') showToast('Connected to ' + stream.channelId, 'success');
    if (stream.state === 'failed') showToast(stream.detail || 'Connection failed', 'error');
});

socket.on('chat_message', (msg) => {
    messageCount += 1;
    msgCounter.textContent = messageCount + ' messages';
    const isGift = msg.eventType === 'gift';
    const item = makeElement('div', 'chat-item flex gap-3 p-3 rounded-lg border ' + (isGift ? 'border-yellow-400/30 bg-yellow-400/[0.06]' : 'border-white/5 bg-white/[0.02]'));
    item.id = 'msg-' + String(msg.id);
    item.dataset.eventType = msg.eventType || 'comment';
    const avatar = makeElement('img', 'w-10 h-10 rounded-full bg-darker object-cover shrink-0');
    avatar.src = safeAvatar(msg.avatar, msg.author);
    avatar.alt = String(msg.author);
    avatar.addEventListener('error', () => { avatar.src = safeAvatar('', msg.author); }, { once: true });
    const content = makeElement('div', 'flex-1 min-w-0');
    const meta = makeElement('div', 'flex items-center gap-2 mb-1');
    meta.appendChild(makeElement('span', 'font-medium text-sm text-gray-200 truncate', msg.author));
    const badgeClass = msg.platform === 'youtube' ? 'platform-badge-yt' : 'platform-badge-tt';
    meta.appendChild(makeElement('span', 'text-[10px] uppercase font-bold px-1.5 py-0.5 rounded ' + badgeClass, msg.platform));
    meta.appendChild(makeElement('span', 'text-[10px] text-gray-500 truncate max-w-[100px] border border-white/5 px-1.5 py-0.5 rounded', msg.channelId));
    if (isGift) meta.appendChild(makeElement('span', 'text-[10px] uppercase font-bold px-1.5 py-0.5 rounded bg-yellow-400/10 text-yellow-300 border border-yellow-400/20', '🎁 GIFT'));
    const commentRow = makeElement('div', 'flex items-start gap-2');
    const giftText = isGift ? GiftRenderer.text(msg) : '';
    GiftRenderer.decorate(commentRow, msg);
    commentRow.appendChild(makeElement('p', 'flex-1 text-sm ' + (isGift ? 'text-yellow-100' : 'text-gray-300') + ' break-words break-all leading-relaxed', renderComment(giftText || msg.comment)));
    const speakButton = makeElement('button', 'shrink-0 mt-0.5 rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[10px] text-gray-400 hover:border-accent/40 hover:text-white', '🔊');
    speakButton.type = 'button';
    speakButton.title = 'อ่านความคิดเห็น';
    speakButton.addEventListener('click', (event) => { event.stopPropagation(); speakComment(msg); });
    commentRow.appendChild(speakButton);
    content.append(meta, commentRow);
    item.append(avatar, content);
    item.addEventListener('contextmenu', (event) => showContextMenu(event, msg));
    item.addEventListener('click', async () => {
        if (item.classList.contains('highlighted')) { await clearHighlight(); return; }
        document.querySelectorAll('.highlighted').forEach((element) => element.classList.remove('highlighted'));
        item.classList.add('highlighted');
        try {
            await fetchJson('/api/highlight', {
                method: 'POST', headers: authenticatedHeaders(), body: JSON.stringify({ action: 'show', msg })
            });
        } catch (error) { showToast(error.message, 'error'); }
    });
    chatContainer.prepend(item);
    if (feedFilter === 'gifts' && !isGift) item.style.display = 'none';
    if (chatContainer.children.length > 200) chatContainer.lastChild.remove();
});

async function clearHighlight() {
    document.querySelectorAll('.highlighted').forEach((element) => element.classList.remove('highlighted'));
    try {
        await fetchJson('/api/highlight', {
            method: 'POST', headers: authenticatedHeaders(), body: JSON.stringify({ action: 'clear' })
        });
    } catch (error) { showToast(error.message, 'error'); }
}

function clearChat() {
    const empty = makeElement('div', 'text-center text-sm text-gray-600 mt-10', 'Feed cleared');
    chatContainer.replaceChildren(empty);
    messageCount = 0;
    msgCounter.textContent = '0 messages';
}

window.addStream = addStream;
window.clearChat = clearChat;
window.clearHighlight = clearHighlight;

if (!security.token) showToast('Missing local access token. Restart the Connector.', 'error');
else fetchJson('/api/streams', { headers: authenticatedHeaders() })
    .then((data) => { data.streams.forEach((stream) => streams.set(stream.streamId, stream)); renderStreams(); })
    .catch((error) => showToast(error.message, 'error'));
