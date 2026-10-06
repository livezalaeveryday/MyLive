# MyLive

Repository เป้าหมาย: https://github.com/livezalaeveryday/MyLive

ซอร์สแอป Windows รวมแชต TikTok LIVE / YouTube Live พร้อม Dashboard และ OBS overlay รวม sponsor ad server ที่ desktop เริ่มให้อัตโนมัติ

## ติดตั้งและรัน

ใช้ Python 3.12 บน Windows และ Microsoft Edge WebView2 Runtime สำหรับหน้าต่าง desktop

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\scripts\run.ps1 -Python .\.venv\Scripts\python.exe
```

ติดตั้งเฉพาะใช้งานด้วย `requirements.txt`; development manifest เพิ่ม PyInstaller และ dependencies ของ optional control-plane tests เวอร์ชันสืบทอดจากต้นทาง ไม่ใช่ transitive lock

`server/server.py` เปิด dashboard และเริ่ม ads port 8787 ค่าเริ่มต้นทั้งสองบริการ bind `0.0.0.0` จึงเข้าถึงผ่าน LAN ได้ ถ้าต้องการเฉพาะเครื่องตั้ง `$env:LCA_BIND_HOST='127.0.0.1'` และ `$env:ADS_BIND_HOST='127.0.0.1'` ก่อนรัน `.env.example` เป็นตัวอย่างเท่านั้น แอปไม่โหลดอัตโนมัติ ให้ตั้ง environment ของ process

Token dashboard สร้างใหม่เมื่อไม่กำหนด `LCA_LOCAL_AUTH_TOKEN` อย่าแชร์ URL ที่มี token ตั้ง `ADS_ADMIN_PASSWORD` ผ่าน environment ส่วนตัว มิฉะนั้น ads server สร้างรหัสชั่วคราวและพิมพ์ออก console

## Build Windows EXE

```powershell
.\scripts\build.ps1 -Python .\.venv\Scripts\python.exe
.\scripts\build.ps1 -Python .\.venv\Scripts\python.exe -AdsOnly
```

ผลลัพธ์อยู่ `dist/`; ชื่อ desktop คงตาม spec เดิม `MyLive_LAN_CopyPaste_v11_GiftsFilter.exe` ทั้งสอง spec อ้าง path จากตำแหน่ง spec จึงเรียกจาก working directory อื่นได้ ระบุ Python ด้วย absolute path เมื่ออยู่นอกโครงการ

สำหรับ EXE onefile ต้องตั้ง `ADS_DATA_DIR` เป็น absolute path ของโฟลเดอร์ข้อมูลถาวรนอก repository เพราะค่าเดิมอิง `__file__/ads_data` และอาจอยู่ในโฟลเดอร์แตกไฟล์ชั่วคราวของ PyInstaller งานนี้คง runtime เดิม ไม่ย้ายข้อมูลจริง

## ตรวจสอบ

```powershell
.\.venv\Scripts\python.exe scripts/check_repository.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

CI รัน source checks และ offline tests บน Windows/Python 3.12 ปรับเฉพาะ expectations ที่ล้าสมัยเรื่อง LAN default และ YouTube handle โดยยังทดสอบปฏิเสธ URL ต่างโดเมน คง security/control-plane tests ที่ยังไม่ผ่านไว้ CI จึงยังเป็นสีแดง ดู `docs/VERIFICATION.md` ยังไม่ยืนยัน live platform, GUI, TTS หรือ EXE บนเครื่องปลายทาง

## ขอบเขต

- `server/`: desktop, ads, static UI และ lca รวม optional control-plane modules/CLI เพื่อความครบของ package
- `tests/`: desktop และ control-plane offline tests ไม่รวม Linux deployment contract test เพราะไม่ได้นำ deployment stack มาด้วย
- `marketing/landing/`: preview พร้อมภาพประกอบ ปุ่มดาวน์โหลดชี้คำแนะนำ build ภายใน ไม่มี binary
- `scripts/`, `.github/`: run/build/check และ CI

Ads server ใช้ HTML ภายในโมดูล ไม่ได้ใช้ `ads_templates/` จึงไม่รวม templates เดิม AdSense fields จัดเก็บค่าเท่านั้น ยังไม่ render โฆษณา AdSense

ไม่รวม `client/`, `WEB COMPANY/`, deployment/runbook หรือรายงานเก่า, credentials, ads_data, logs, caches และ EXE ไม่พบ Firebase imports ใน runtime ที่นำมา จึงตัด firebase-admin; requests ไม่ใช่ direct dependency ของ runtime นี้

Repository นี้เก็บ source บน `main` โดยรักษาประวัติ initial commit เดิม ยังไม่ได้เลือก license ดูขั้นตอนพัฒนาต่อใน [คู่มือ GitHub](docs/GITHUB.md) URLs ของ ads/CDN เดิมยังอยู่เพื่อรักษาพฤติกรรม Optional control plane ไม่มี deployment package ในชุดนี้
