"""Dependency-free sponsor/ad server for ads.finstall.click."""
from __future__ import annotations
import html, hmac, json, os, secrets, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT=Path(__file__).resolve().parent; DATA_DIR=Path(os.getenv('ADS_DATA_DIR',ROOT/'ads_data')); DATA_DIR.mkdir(parents=True,exist_ok=True)
DB_PATH=DATA_DIR/'ads.sqlite3'; HOST=os.getenv('ADS_BIND_HOST','0.0.0.0'); PORT=int(os.getenv('ADS_PORT','8787'))
ADMIN_PASSWORD=os.getenv('ADS_ADMIN_PASSWORD','') or secrets.token_urlsafe(12)
if not os.getenv('ADS_ADMIN_PASSWORD'): print(f'[ads] Temporary admin password: {ADMIN_PASSWORD}')
DEFAULT={'enabled':'1','title':'สนใจลงโฆษณาสปอนเซอร์กับเรา','description':'เข้าถึงกลุ่มผู้สร้างคอนเทนต์และนักไลฟ์','contact':'ติดต่อทีมงานผ่าน ads.finstall.click','button_text':'ติดต่อทีมงาน','button_url':'https://ads.finstall.click/','banner_url':'','adsense_enabled':'0','adsense_client':'','adsense_slot':''}
def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)'); c.executemany('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',DEFAULT.items())
def get_settings():
    with sqlite3.connect(DB_PATH) as c: data=dict(c.execute('SELECT key,value FROM settings'))
    return {**DEFAULT,**data}
def esc(v): return html.escape(str(v or ''),quote=True)
def page(body): return '<!doctype html><html lang="th"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'+body
def ad_html(ad):
    content=(f'<a href="{esc(ad["button_url"])}" target="_blank" rel="noopener"><img src="{esc(ad["banner_url"])}" alt="Sponsor"></a>' if ad['banner_url'] else f'<h3>{esc(ad["title"])}</h3><p>{esc(ad["description"])}</p><p>{esc(ad["contact"])}</p><a href="{esc(ad["button_url"])}" target="_blank" rel="noopener">{esc(ad["button_text"])}</a>')
    return page('<style>html,body{width:100%;height:100%;overflow:hidden}body{margin:0;background:#18181b;color:#eee;font-family:system-ui}.card{box-sizing:border-box;height:100%;max-height:100%;overflow:hidden;padding:clamp(9px,2.2vw,16px) clamp(11px,2.6vw,18px);border:1px solid #3f3f46;border-radius:12px;background:linear-gradient(135deg,#21183d,#18181b)}h3{margin:0 0 clamp(3px,1vw,7px);color:#c4b5fd;font-size:clamp(13px,2.5vw,18px);line-height:1.2;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}p{margin:clamp(2px,0.8vw,5px) 0;color:#d4d4d8;font-size:clamp(10px,1.8vw,13px);line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}a{display:inline-block;margin-top:clamp(4px,1vw,8px);padding:clamp(5px,1.2vw,8px) clamp(8px,2vw,13px);background:#8b5cf6;color:#fff;border-radius:7px;text-decoration:none;font-size:clamp(10px,1.8vw,13px)}img{display:block;max-width:100%;max-height:100%;border-radius:8px}</style><div class="card">'+content+'</div>')
class Handler(BaseHTTPRequestHandler):
    def send(self,body,status=200,ctype='text/html; charset=utf-8'):
        raw=body if isinstance(body,bytes) else body.encode(); self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(raw)
    def read_form(self):
        n=int(self.headers.get('Content-Length','0')); return {k:v[0] for k,v in parse_qs(self.rfile.read(n).decode()).items()}
    def do_GET(self):
        path=urlparse(self.path).path; ad=get_settings()
        if path=='/':
            self.send('<meta http-equiv="refresh" content="0; url=/ad/livechat">'); return
        if path=='/health': return self.send(json.dumps({'status':'ok','service':'sponsor-ads'}),ctype='application/json')
        if path=='/api/ads/active': return self.send(json.dumps({**ad,'provider':'sponsor' if ad['enabled']=='1' else 'fallback'},ensure_ascii=False),ctype='application/json')
        if path=='/ad/livechat': return self.send(ad_html(ad))
        if path=='/admin': return self.send(self.admin_page(ad))
        self.send('Not Found',404)
    def do_POST(self):
        path=urlparse(self.path).path; form=self.read_form()
        if path=='/admin/login':
            if hmac.compare_digest(form.get('password',''),ADMIN_PASSWORD): return self.send('<script>location.href="/admin?ok=1"</script>')
            return self.send(self.login_page('รหัสผ่านไม่ถูกต้อง'),401)
        if path=='/admin/save':
            if not hmac.compare_digest(form.get('admin_password',''),ADMIN_PASSWORD): return self.send('Unauthorized',401)
            values={k:form.get(k,'') for k in DEFAULT}; values['enabled']='1' if form.get('enabled') else '0'; values['adsense_enabled']='1' if form.get('adsense_enabled') else '0'
            with sqlite3.connect(DB_PATH) as c: c.executemany('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',values.items())
            return self.send('<script>location.href="/admin?saved=1"</script>')
        self.send('Not Found',404)
    def login_page(self,error=''): return page(f'<body style="font-family:system-ui;max-width:420px;margin:60px auto;background:#111;color:#eee;padding:24px"><h1>Ads Admin</h1><form method="post" action="/admin/login"><input name="password" type="password" required placeholder="รหัสผ่าน" style="width:100%;padding:10px"><button style="margin-top:12px;padding:10px">เข้าสู่ระบบ</button></form><p style="color:#f88">{esc(error)}</p></body>')
    def admin_page(self,ad):
        fields=[('title','หัวข้อ'),('description','รายละเอียด'),('contact','ช่องทางติดต่อ'),('button_text','ข้อความปุ่ม'),('button_url','ลิงก์ปุ่ม'),('banner_url','URL รูป Banner')]
        inputs=''.join(f'<p><label>{label}<br><input name="{key}" value="{esc(ad[key])}" style="width:100%;padding:9px"></label></p>' for key,label in fields)
        return page(f'<body style="font-family:system-ui;max-width:760px;margin:30px auto;background:#111;color:#eee;padding:24px"><h1>จัดการโฆษณา Sponsor</h1><p style="color:#8f8">{"บันทึกแล้ว" if "saved=1" in self.path else ""}</p><form method="post" action="/admin/save"><input name="admin_password" type="password" placeholder="รหัสผ่านผู้ดูแล" required style="width:100%;padding:9px"><p><label><input type="checkbox" name="enabled" value="1" {"checked" if ad["enabled"]=="1" else ""}> เปิด Sponsor Message</label></p>{inputs}<hr><h2>AdSense (เตรียมไว้)</h2><p><label><input type="checkbox" name="adsense_enabled" value="1"> เปิดใช้เมื่อได้รับอนุมัติ</label></p><p>Publisher Client<br><input name="adsense_client" value="{esc(ad["adsense_client"])}" style="width:100%;padding:9px"></p><p>Ad Slot<br><input name="adsense_slot" value="{esc(ad["adsense_slot"])}" style="width:100%;padding:9px"></p><button style="padding:10px 20px">บันทึก</button></form></body>')
def create_server():
    init_db()
    return ThreadingHTTPServer((HOST, PORT), Handler)

def run_server():
    create_server().serve_forever()

if __name__=='__main__': run_server()
