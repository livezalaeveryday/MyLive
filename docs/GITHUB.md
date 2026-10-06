# นำ MyLive ขึ้น GitHub

ปลายทาง: https://github.com/livezalaeveryday/MyLive (Private ตรวจเมื่อ 2026-10-06)

โฟลเดอร์ทำงาน: `F:\MyLive` เชื่อม `origin` และให้ `main` ติดตาม `origin/main` ประวัติ initial commit เดิมถูกเก็บไว้ ขั้นตอนด้านล่างใช้สำหรับส่งการเปลี่ยนแปลงครั้งต่อไป

## ตรวจและส่ง source

เปิด PowerShell ในโฟลเดอร์โปรเจกต์:

```powershell
git remote -v
git status --short
py -3.12 scripts/check_repository.py
git add .
git diff --cached --stat
git diff --cached --check
git commit -m "Organize MyLive source, documentation and build tooling"
git push origin main
```

หาก Git ขอชื่อและอีเมล ให้ตั้งตัวตนผู้เขียนด้วย `git config user.name` และ `git config user.email` ภายใน repository นี้ก่อน commit ใช้อีเมล GitHub noreply ได้หากต้องการเก็บอีเมลส่วนตัว

หากมีคนเพิ่ม commit บน GitHub ระหว่างเตรียมงาน ให้ fetch และตรวจความเปลี่ยนแปลงก่อนรวมประวัติ ไม่ใช้ force push

อ่าน [ผลตรวจ](VERIFICATION.md) ก่อนส่ง: ยังมี tests เดิมไม่ผ่าน 3 จุด จึงยังไม่ใช่ release ที่ผ่านทุกเกณฑ์ CI จะรายงานข้อผิดพลาดเหล่านี้ตามจริง

## ไฟล์ดาวน์โหลดและข้อมูลส่วนตัว

เก็บ EXE สำหรับผู้ใช้ไว้ใน GitHub Releases หลังทดสอบการใช้งานแล้ว; `.gitignore` กัน `dist/` และ `build/` ออกจาก source ปุ่มดาวน์โหลดบน landing preview ชี้คู่มือ build จนกว่าจะมี release จริง

ไม่ส่ง `.env`, Firebase keys, passwords, ฐานข้อมูล Ads, logs หรือข้อมูลผู้ใช้ขึ้น GitHub ใช้ `.env.example` เป็นรายการอ้างอิง และตั้งค่าจริงผ่าน environment ของเครื่อง แอปไม่ได้โหลด `.env` อัตโนมัติ
