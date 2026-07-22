# -*- coding: utf-8 -*-
"""
บอทแจ้งเตือนราคาน้ำมัน (ขึ้น/ลง) + รูปภาพสรุป
------------------------------------------------
ขั้นตอน: ดึงข้อมูล (API บางจาก) -> ตรวจว่าราคาเปลี่ยนไหม -> สร้างรูป (Pillow) -> ส่งเข้า Telegram / LINE

ใช้ได้กับทั้ง Telegram Bot และ LINE Messaging API (LINE Notify ปิดตัวไปแล้วตั้งแต่ 31 มี.ค. 2025)

ติดตั้งไลบรารี:
    pip install requests pillow

ตั้งค่าผ่าน Environment Variable (แนะนำ) หรือแก้ค่าในไฟล์ config.py
"""

import os
import io
import json
import requests
from PIL import Image, ImageDraw, ImageFont

# ============================================================
# 1) ตั้งค่า (ใส่ token ของคุณ) — อ่านจาก Environment Variable ก่อน
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")   # จาก @BotFather
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")     # chat id ปลายทาง

LINE_ACCESS_TOKEN  = os.getenv("LINE_ACCESS_TOKEN", "")    # Channel access token (Messaging API)
LINE_TARGET_ID     = os.getenv("LINE_TARGET_ID", "")       # groupId ของกลุ่ม (หรือ userId) ปลายทาง
IMGBB_API_KEY      = os.getenv("IMGBB_API_KEY", "")        # api key จาก imgbb.com สำหรับอัปโหลดรูป

# แหล่งข้อมูล: Web Service ทางการของบางจาก (ฟรี, คืนค่าเป็น JSON)
BANGCHAK_API = "https://oil-price.bangchak.co.th/ApiOilPrice2/th"

# แปลงชื่อแบรนด์ของบางจาก -> ชื่อมาตรฐานที่คนทั่วไปเรียก (อ่านง่ายขึ้น)
NAME_MAP = {
    "ไฮดีเซล S": "ดีเซล B7",
    "ดีเซล B20": "ดีเซล B20",
    "ไฮ พรีเมียม ดีเซล พลัส": "ดีเซล พรีเมียม",
    "ไฮ พรีเมียม 98 พลัส": "แก๊สโซฮอล์ 98",
    "แก๊สโซฮอล์ 95 S EVO": "แก๊สโซฮอล์ 95",
    "แก๊สโซฮอล์ 91 S EVO": "แก๊สโซฮอล์ 91",
    "แก๊สโซฮอล์ E20 S EVO": "แก๊สโซฮอล์ E20",
    "แก๊สโซฮอล์ E85 S EVO": "แก๊สโซฮอล์ E85",
}

# เก็บราคาล่าสุดที่เคยแจ้ง เพื่อเทียบว่ามีการเปลี่ยนแปลงไหม
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_prices.json")

# ฟอนต์ไทย — ใส่ path ฟอนต์ .ttf ที่รองรับภาษาไทย (เช่น Sarabun, TH Sarabun New, Noto Sans Thai)
FONT_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "Sarabun-Regular.ttf"),
    "/usr/share/fonts/truetype/tlwg/Sarabun.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
]
FONT_BOLD_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "Sarabun-Bold.ttf"),
    "/usr/share/fonts/truetype/tlwg/Sarabun-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansThai-Bold.ttf",
    "C:/Windows/Fonts/tahomabd.ttf",
]


# ============================================================
# 2) ดึงข้อมูลราคาน้ำมัน
# ============================================================
def fetch_prices():
    """ดึงราคาน้ำมันจาก API บางจาก แล้วคืนค่าเป็น dict ที่ใช้ง่าย"""
    r = requests.get(BANGCHAK_API, timeout=20)
    r.raise_for_status()
    root = r.json()[0]                       # API คืนค่าเป็น list ที่มี object เดียว
    oils = json.loads(root["OilList"])       # OilList เป็น string JSON ต้อง parse ซ้ำอีกชั้น

    items = []
    for o in oils:
        raw_name = o["OilName"].strip()
        items.append({
            # ใช้ชื่อมาตรฐานถ้ามีในตาราง ถ้าไม่มีก็ใช้ชื่อเดิมจากบางจาก
            "name":       NAME_MAP.get(raw_name, raw_name),
            "today":      float(o["PriceToday"]),
            "tomorrow":   float(o["PriceTomorrow"]),
            # ผลต่างของ "พรุ่งนี้เทียบวันนี้" > 0 = ขึ้น, < 0 = ลง (ข้อมูลล่วงหน้า)
            "diff":       float(o["PriceDifTomorrow"]),
        })

    return {
        "date_now":    root.get("OilDateNow", ""),     # วันที่ปัจจุบัน
        "price_date":  root.get("OilPriceDate", ""),   # วันที่ประกาศราคา
        "effective":   root.get("OilRemark2", ""),     # เช่น "ราคามีผล ณ วันที่ 8 ก.ค. 69 เวลา 05.00 น."
        "remark":      root.get("OilRemark", ""),
        "items":       items,
    }


# ============================================================
# 3) ตรวจว่าราคาเปลี่ยนแปลงหรือไม่ (กันสแปม แจ้งเฉพาะตอนมีการปรับ)
# ============================================================
def load_last_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(data):
    snapshot = {it["name"]: it["tomorrow"] for it in data["items"]}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


def has_price_change(data):
    """เทียบราคา 'พรุ่งนี้' กับที่เคยบันทึกไว้ครั้งก่อน"""
    last = load_last_state()
    if not last:
        return True  # รันครั้งแรก ให้แจ้งเลย
    for it in data["items"]:
        if abs(last.get(it["name"], -1) - it["tomorrow"]) > 0.001:
            return True
    return False


# ============================================================
# 4) สร้างข้อความสรุป
# ============================================================
def arrow(diff):
    if diff > 0:
        return "🔺 +{:.2f}".format(diff)
    if diff < 0:
        return "🔻 {:.2f}".format(diff)
    return "➖ คงที่"


def build_message(data):
    lines = ["⛽ อัปเดตราคาน้ำมัน (บางจาก)"]
    if data["effective"]:
        lines.append(data["effective"])
    lines.append("")
    for it in data["items"]:
        lines.append("{:<22s} {:>6.2f}  {}".format(it["name"], it["tomorrow"], arrow(it["diff"])))
    lines.append("")
    lines.append("ข้อมูล: บางจาก | ราคา กทม. ยังไม่รวมภาษีท้องถิ่น")
    return "\n".join(lines)


# ============================================================
# 5) สร้างรูปภาพสรุปด้วย Pillow
# ============================================================
def _load_font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    # fallback: ฟอนต์เริ่มต้น (อาจแสดงภาษาไทยไม่สวย — แนะนำให้ใส่ไฟล์ฟอนต์ไทย)
    return ImageFont.load_default()


def make_image(data, out_path="oil_price.png"):
    W = 720
    row_h = 58
    header_h = 170
    footer_h = 70
    H = header_h + row_h * len(data["items"]) + footer_h

    bg      = (17, 24, 39)      # กรมท่าเข้ม
    card    = (31, 41, 55)
    white   = (243, 244, 246)
    gray    = (156, 163, 175)
    up_col  = (248, 113, 113)   # แดง = ขึ้น
    down_col= (52, 211, 153)    # เขียว = ลง
    flat_col= (156, 163, 175)

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    f_title = _load_font(FONT_BOLD_CANDIDATES, 40)
    f_sub   = _load_font(FONT_CANDIDATES, 24)
    f_name  = _load_font(FONT_CANDIDATES, 28)
    f_price = _load_font(FONT_BOLD_CANDIDATES, 32)
    f_diff  = _load_font(FONT_CANDIDATES, 24)
    f_foot  = _load_font(FONT_CANDIDATES, 20)

    # หัวข้อ
    d.text((30, 30), "⛽ ราคาน้ำมันบางจาก", font=f_title, fill=white)
    d.text((30, 90), data["effective"] or "ราคาล่าสุด", font=f_sub, fill=gray)
    d.line((30, 150, W - 30, 150), fill=(55, 65, 81), width=2)

    y = header_h
    for i, it in enumerate(data["items"]):
        if i % 2 == 0:
            d.rectangle((20, y, W - 20, y + row_h - 6), fill=card)

        d.text((36, y + 14), it["name"], font=f_name, fill=white)

        price_txt = "{:.2f}".format(it["tomorrow"])
        d.text((W - 300, y + 12), price_txt, font=f_price, fill=white)

        if it["diff"] > 0:
            col, txt = up_col, "▲ +{:.2f}".format(it["diff"])
        elif it["diff"] < 0:
            col, txt = down_col, "▼ {:.2f}".format(it["diff"])
        else:
            col, txt = flat_col, "— คงที่"
        d.text((W - 170, y + 16), txt, font=f_diff, fill=col)

        y += row_h

    d.text((30, H - 50), "ข้อมูล: บางจาก · ราคา กทม. ยังไม่รวมภาษีบำรุงท้องถิ่น",
           font=f_foot, fill=gray)

    img.save(out_path)
    return out_path


# ============================================================
# 6) ส่งเข้า Telegram (ข้อความ + รูป)
# ============================================================
def send_telegram(text, image_path=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] ข้ามการส่ง — ยังไม่ได้ตั้งค่า token/chat_id")
        return
    base = "https://api.telegram.org/bot{}".format(TELEGRAM_BOT_TOKEN)
    if image_path:
        with open(image_path, "rb") as photo:
            resp = requests.post(
                base + "/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": text},
                files={"photo": photo}, timeout=30)
    else:
        resp = requests.post(
            base + "/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=30)
    print("[Telegram]", resp.status_code, resp.text[:120])


# ============================================================
# 7a) อัปโหลดรูปขึ้น imgbb เพื่อให้ได้ URL https (LINE ต้องใช้ URL ส่งไฟล์ตรงไม่ได้)
# ============================================================
def upload_image_imgbb(image_path):
    if not IMGBB_API_KEY:
        print("[imgbb] ยังไม่ได้ตั้ง IMGBB_API_KEY — ส่ง LINE แบบมีรูปไม่ได้")
        return None
    import base64
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    resp = requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": IMGBB_API_KEY, "image": b64},
        timeout=30)
    if resp.status_code == 200:
        url = resp.json()["data"]["url"]
        print("[imgbb] อัปโหลดสำเร็จ:", url)
        return url
    print("[imgbb] อัปโหลดล้มเหลว", resp.status_code, resp.text[:120])
    return None


# ============================================================
# 7b) ส่งเข้า LINE กลุ่ม (Messaging API — push message)
#     LINE_TARGET_ID ใส่เป็น groupId ของกลุ่ม (หรือ userId ก็ได้)
# ============================================================
def send_line(text, image_url=None):
    if not LINE_ACCESS_TOKEN or not LINE_TARGET_ID:
        print("[LINE] ข้ามการส่ง — ยังไม่ได้ตั้งค่า token/target id")
        return
    messages = [{"type": "text", "text": text}]
    if image_url:
        messages.append({
            "type": "image",
            "originalContentUrl": image_url,
            "previewImageUrl": image_url,
        })
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + LINE_ACCESS_TOKEN,
        },
        data=json.dumps({"to": LINE_TARGET_ID, "messages": messages}),
        timeout=30)
    print("[LINE]", resp.status_code, resp.text[:120])


# ============================================================
# 8) ฟังก์ชันหลัก
# ============================================================
def main(force=False):
    data = fetch_prices()

    if not force and not has_price_change(data):
        print("ราคาน้ำมันไม่เปลี่ยนแปลง — ไม่ส่งแจ้งเตือน")
        return

    text = build_message(data)
    img_path = make_image(data, os.path.join(os.path.dirname(os.path.abspath(__file__)), "oil_price.png"))

    # --- ส่งเข้า LINE กลุ่ม (ช่องทางหลัก) ---
    image_url = upload_image_imgbb(img_path)   # อัปรูปขึ้น imgbb ก่อน -> ได้ URL
    send_line(text, image_url=image_url)

    # --- ส่ง Telegram ด้วย (ถ้าตั้งค่าไว้ จะแนบไฟล์รูปตรง ๆ) ---
    send_telegram(text, img_path)

    save_state(data)
    print("แจ้งเตือนเรียบร้อย")


if __name__ == "__main__":
    import sys
    main(force="--force" in sys.argv)
