"""Local browser fixture with one remote CDN image; no platform connections or real credentials."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import re

ROOT = Path(__file__).resolve().parents[1] / 'server/static'
events = [
    {'id':'fixture-rose','platform':'tiktok','channelId':'fixture','author':'Rose Viewer','eventType':'gift','giftName':'Rose','giftCount':10,'giftImageUrl':'https://p16-webcast.tiktokcdn.com/img/maliva/webcast-va/eba3a9bb85c33e017f3648eaf88d7189~tplv-obj.webp','comment':'Rose'},
    {'id':'fixture-dove','platform':'tiktok','channelId':'fixture','author':'Dove Viewer','eventType':'gift','giftName':'White Dove','giftCount':3,'comment':'White Dove'},
    {'id':'fixture-unknown','platform':'tiktok','channelId':'fixture','author':'Unknown Viewer','eventType':'gift','giftName':'Unknown Gift','giftId':'5','giftCount':1,'comment':'Unknown Gift'},
    {'id':'fixture-yt','platform':'youtube','channelId':'dQw4w9WgXcQ','author':'YT Viewer','eventType':'comment','comment':'YouTube comment'},
]
mock = '<script>window.io=()=>({emit(){},on(name,callback){if(name==="chat_message")setTimeout(()=>'+json.dumps(events)+'.forEach(callback),500);if(name==="highlight_message")setTimeout(()=>callback({action:"show",msg:'+json.dumps(events[0])+'}),500);}});</script>'

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(ROOT),**kwargs)
    def do_GET(self):
        path = self.path.split('?')[0]
        if path.startswith('/api/'):
            data = b'{"status":"ready","streams":[]}'
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(data)
        elif path in ('/','/overlay.html','/overlay_highlight.html'):
            source = (ROOT/('index.html' if path=='/' else path[1:])).read_text(encoding='utf-8')
            source = re.sub(r'<script src="https://cdn.socket.io/[^>]+></script>',lambda _:mock,source)
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.end_headers(); self.wfile.write(source.encode())
        else:
            super().do_GET()

if __name__=='__main__':
    print('Gift fixture http://127.0.0.1:8766 (mock events; remote CDN image)',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8766),Handler).serve_forever()
