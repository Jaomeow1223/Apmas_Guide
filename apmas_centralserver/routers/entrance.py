from fastapi import APIRouter, File, UploadFile, Form
from database import get_tickets_ref
import uuid, datetime, aiofiles, os

router = APIRouter()

SERVER_BASE_URL = "http://10.153.161.199:8000"
IMAGE_DIR = "static/images"

@router.post("/upload_image")
async def upload_image(
    file:      UploadFile = File(...),
    ticket_id: str = Form(default="unknown"),
    label:     str = Form(default="plate"),
):
    """Pi4 อัปโหลดรูปมาเก็บที่ Server แล้วคืน URL รูปแบบ /images/{filename}"""
    os.makedirs(IMAGE_DIR, exist_ok=True)
    filename = f"{ticket_id}_{label}.jpg"
    path     = f"{IMAGE_DIR}/{filename}"
    async with aiofiles.open(path, "wb") as f:
        await f.write(await file.read())

    # URL รูปแบบที่ต้องการ: http://10.153.161.199:8000/images/{filename}
    url = f"{SERVER_BASE_URL}/images/{filename}"
    return {"success": True, "url": url, "filename": filename}


@router.post("/entrance/capture")
async def capture(
    plate_text:  str        = Form(...),
    source:      str        = Form(default="entrance_pi4"),
    timestamp:   str        = Form(default=None),
    image_plate: UploadFile = File(default=None),
):
    """รับข้อมูลป้ายทะเบียน (ไม่มีจังหวัดแล้ว)"""
    ticket_id = "TK" + uuid.uuid4().hex[:8].upper()
    time_in   = timestamp or datetime.datetime.now().isoformat()

    image_plate_url = None
    if image_plate:
        os.makedirs(IMAGE_DIR, exist_ok=True)
        filename = f"{ticket_id}_plate.jpg"
        path     = f"{IMAGE_DIR}/{filename}"
        async with aiofiles.open(path, "wb") as f:
            await f.write(await image_plate.read())
        image_plate_url = f"{SERVER_BASE_URL}/images/{filename}"

    get_tickets_ref().child(ticket_id).set({
        "ticket_id":           ticket_id,
        "plate_text_raw":      plate_text,
        "plate_text_verified": None,
        "province_raw":        "",
        "province_verified":   None,
        "time_in":             time_in,
        "time_out":            None,
        "slot_id":             "Not assigned",
        "status":              "pending",
        "image_car_url":       None,
        "image_plate_url":     image_plate_url,
        "qr_url":              None,
        "source":              source
    })

    return {"success": True, "ticket_id": ticket_id}
