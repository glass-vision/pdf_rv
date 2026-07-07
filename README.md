# PDF RV Application

เว็บแอปสำหรับจัดการเอกสาร PDF ผ่านหน้าเว็บ พร้อมการบันทึกข้อมูลที่เกี่ยวข้องกับเอกสารในฐานข้อมูลของระบบ

## ความสามารถหลัก

* จัดการเอกสาร PDF ผ่านหน้าเว็บ
* บันทึกและแสดงข้อมูลเอกสาร
* ค้นหาและตรวจสอบรายการเอกสาร
* ตั้งค่าการทำงานผ่าน environment variables
* รองรับการรันด้วย Python
* รองรับการรันด้วย Docker Compose

## โครงสร้างโปรเจกต์

```text
app/
  main.py
  config.py
  database.py
  routers/
  services/
  static/
  templates/

Dockerfile
docker-compose.yml
requirements.txt
.env.example
README.md
```

## สิ่งที่ต้องมี

* Python 3.11 หรือใหม่กว่า
* Docker และ Docker Compose สำหรับการรันผ่าน container

## การตั้งค่า Environment

สร้างไฟล์ configuration สำหรับเครื่อง local จากไฟล์ตัวอย่าง:

```bash
cp .env.example .env
```

จากนั้นปรับค่าตามสภาพแวดล้อมที่ต้องการใช้งาน

## การรันด้วย Docker Compose

```bash
docker compose up --build
```

จากนั้นเปิดเว็บเบราว์เซอร์ที่:

```text
http://localhost:8000
```

หากมีการกำหนด port อื่น ให้ใช้งานตามค่าที่กำหนดไว้ใน configuration ของระบบ

## การรันแบบ Local ด้วย Python

สร้าง virtual environment:

```bash
python -m venv .venv
```

เปิดใช้งาน virtual environment

สำหรับ macOS หรือ Linux:

```bash
source .venv/bin/activate
```

สำหรับ Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

ติดตั้ง dependencies:

```bash
pip install -r requirements.txt
```

เริ่มรันแอป:

```bash
uvicorn app.main:app --reload
```

จากนั้นเปิดเว็บเบราว์เซอร์ที่:

```text
http://localhost:8000
```

## การใช้งานเบื้องต้น

หลังจากเปิดระบบแล้ว สามารถเข้าใช้งานผ่านเว็บเบราว์เซอร์เพื่อจัดการเอกสารและดูข้อมูลที่ระบบบันทึกไว้

