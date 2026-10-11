'use strict';
const pageNames = {dashboard:'Dashboard',connections:'Connections',events:'Events',triggers:'Triggers',overlay:'Overlay',sound:'Sound / TTS',obs:'OBS',settings:'Settings'};
function showPage(page) {
    if (!pageNames[page]) page = 'dashboard';
    document.querySelectorAll('[data-views]').forEach(section => { section.hidden = !section.dataset.views.split(' ').includes(page); });
    document.querySelectorAll('.navigation [data-page]').forEach(button => {
        if (button.dataset.page === page) button.setAttribute('aria-current', 'page');
        else button.removeAttribute('aria-current');
    });
    document.getElementById('page-title').textContent = pageNames[page];
    document.getElementById('page-description').textContent = page === 'dashboard' ? 'Your live streams, messages and overlays in one place.' : 'Manage your ' + pageNames[page].toLowerCase() + '.';
    document.getElementById('page-title').tabIndex = -1;
    document.getElementById('page-title').focus();
}
document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => showPage(button.dataset.page)));
document.querySelectorAll('[data-connect-platform]').forEach(button => button.addEventListener('click', () => {
    showPage('connections');
    document.getElementById('platform-select').value = button.dataset.connectPlatform;
    document.getElementById('channel-id').focus();
}));
document.getElementById('connect-stream').addEventListener('click', addStream);
document.getElementById('channel-id').addEventListener('keydown', event => { if (event.key === 'Enter') addStream(); });
document.getElementById('clear-highlight').addEventListener('click', clearHighlight);
document.getElementById('clear-feed').addEventListener('click', clearChat);
function refreshPlatformStatus() {
    const empty = document.getElementById('feed-empty');
    if (empty) empty.textContent = streams.size ? 'Waiting for live messages. Previous feed is not restored after refresh.' : 'Connect a stream to start receiving messages.';
    for (const platform of ['youtube','tiktok']) {
        const records = [...streams.values()].filter(stream => stream.platform === platform);
        const connected = records.filter(stream => stream.state === 'connected').length;
        const output = document.getElementById(platform + '-status');
        output.textContent = connected ? connected + ' connected' : records.some(stream => stream.state === 'failed') ? 'Connection failed' : records.length ? 'Connecting…' : 'Not connected';
    }
}
new MutationObserver(refreshPlatformStatus).observe(activeStreamsList, {childList:true,subtree:true});
socket.on('connect', () => { document.getElementById('socket-status').textContent = 'Connected'; refreshPlatformStatus(); });
socket.on('disconnect', () => {
    document.getElementById('socket-status').textContent = 'Disconnected';
    for (const platform of ['youtube','tiktok']) document.getElementById(platform + '-status').textContent = 'Status unavailable';
});
socket.on('connect_error', () => { document.getElementById('socket-status').textContent = 'Unavailable'; });
socket.on('chat_message', () => {
    document.getElementById('feed-empty')?.remove();
    chatContainer.querySelector('[data-page]')?.remove();
    document.getElementById('preview-state').textContent = 'Live chat source · receiving messages';
});
async function refreshSystemStatus() {
    const output = document.getElementById('server-status');
    output.textContent = 'Checking…';
    try {
        const status = await fetchJson('/api/status', {headers:authenticatedHeaders()});
        output.textContent = status.status === 'ready' ? 'Ready' : status.status;
        document.getElementById('auth-status').textContent = 'Authenticated';
    } catch (_error) {
        output.textContent = 'Unavailable';
        document.getElementById('auth-status').textContent = security.token ? 'Access unavailable' : 'Token missing';
    }
}
document.getElementById('refresh-status').addEventListener('click', refreshSystemStatus);
const preview = document.getElementById('overlay-preview');
if (!security.token) document.getElementById('socket-status').textContent = 'Token required';
else if (!socketAvailable) document.getElementById('socket-status').textContent = 'Library unavailable';
if (security.token) {
    preview.src = overlayUrl(overlayProfiles[0]);
    document.getElementById('preview-state').textContent = 'Live chat source · waiting for messages';
} else document.getElementById('preview-state').textContent = 'Restart the app to restore local access.';
document.querySelectorAll('[data-zoom="chat"], [data-reset-zoom="chat"]').forEach(button => button.addEventListener('click', () => { if (security.token) preview.src = overlayUrl(overlayProfiles[0]); }));
const rate = document.getElementById('tts-rate');
rate.value = String(ttsRate);
document.getElementById('tts-rate-value').textContent = ttsRate.toFixed(1) + 'x';
rate.addEventListener('input', () => {
    ttsRate = Number(rate.value);
    localStorage.setItem('lca.tts.rate', String(ttsRate));
    document.getElementById('tts-rate-value').textContent = ttsRate.toFixed(1) + 'x';
});
function refreshVoiceStatus() {
    chooseThaiVoice();
    document.getElementById('voice-status').textContent = !tts ? 'Speech is unavailable in this browser.' : thaiVoice ? 'Thai voice ready: ' + thaiVoice.name : 'No Thai voice detected. Install one in Windows.';
}
if (tts) tts.addEventListener('voiceschanged', refreshVoiceStatus);
refreshVoiceStatus();
refreshPlatformStatus();
refreshSystemStatus();
