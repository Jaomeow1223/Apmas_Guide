from fastapi import APIRouter, HTTPException
from database import get_slots_ref, get_slot_ref, get_ticket_ref
from models import SlotStatusPayload
import datetime

router = APIRouter()

# ── ดึงสถานะช่องจอดทั้งหมด ──
@router.get("/slots")
def get_all_slots():
    data = get_slots_ref().get() or {}
    return data

# ── Pi5 อัปเดตสถานะช่องจอด ──
# เรียกเมื่อ: sensor ตรวจเจอรถ หรือ รถออกแล้ว
@router.patch("/slots/{slot_id}")
def update_slot(slot_id: str, payload: SlotStatusPayload):
    slot_ref = get_slot_ref(slot_id)
    slot     = slot_ref.get()

    if not slot:
        raise HTTPException(status_code=404, detail=f"ไม่พบช่องจอด {slot_id}")

    now = datetime.datetime.now().isoformat()

    # ── รถเข้าช่อง: ตรวจสอบป้ายทะเบียน ──
    if payload.status == "occupied" and payload.plate_text:
        plate = payload.plate_text.strip()

        # หา ticket ที่ verified และ slot_id ตรงกัน
        from database import get_tickets_ref
        tickets_data = get_tickets_ref().get() or {}

        matched_ticket = None
        for tid, t in tickets_data.items():
            # รับทุก status ที่รถเข้าได้: slot_assigned, verified, parked
            valid_status = t.get("status") in ("slot_assigned", "verified", "parked", "pending")
            if t.get("status") == "exited": continue   # รถออกแล้ว ไม่เทียบ
            if not valid_status: continue
            if t.get("slot_id") != slot_id: continue

            # เทียบป้าย: ใช้ plate_text_verified ก่อน ถ้าไม่มีใช้ plate_text_raw
            # กรอง "None" string (Firebase บางครั้งเก็บ string "None" แทน null)
            def _clean(v):
                if not v or str(v).strip().lower() in ("none", "null", ""):
                    return ""
                return str(v).replace(" ", "").strip()

            stored_plate = _clean(t.get("plate_text_verified")) or                            _clean(t.get("plate_text_raw"))

            if stored_plate and stored_plate == plate.replace(" ", ""):
                matched_ticket = t
                matched_tid    = tid
                break

        if matched_ticket:
            # ✅ ตรงกัน → อัปเดตทั้ง slot และ ticket
            slot_ref.update({
                "status":     "occupied",
                "ticket_id":  matched_tid,
                "plate":      plate,
                "time_in":    now
            })
            get_ticket_ref(matched_tid).update({
                "status":   "parked",
                "slot_id":  slot_id
            })
            return {
                "match": True,
                "slot_id": slot_id,
                "ticket_id": matched_tid,
                "message": f"✅ {plate} จอดถูกช่อง {slot_id}"
            }
        else:
            # ❌ ไม่ตรง → แจ้งเตือน ไม่เปลี่ยน slot
            return {
                "match": False,
                "slot_id": slot_id,
                "plate":   plate,
                "message": f"❌ {plate} ไม่ตรงกับช่อง {slot_id}"
            }

    # ── รถออกจากช่อง ──
    elif payload.status == "free":
        ticket_id = slot.get("ticket_id")
        slot_ref.update({
            "status":    "free",
            "ticket_id": None,
            "plate":     None,
            "time_in":   None
        })
        return {
            "match": True,
            "slot_id": slot_id,
            "message": f"✅ ช่อง {slot_id} ว่างแล้ว"
        }

    # ── Pi5 แจ้งสถานะ sensor เฉยๆ (ไม่มีป้าย) ──
    else:
        slot_ref.update({"status": payload.status})
        return {"slot_id": slot_id, "status": payload.status}


# ── Admin กำหนดช่องจอดให้ ticket ──
@router.post("/slots/assign")
def assign_slot(ticket_id: str, slot_id: str):
    ticket_ref = get_ticket_ref(ticket_id)
    ticket     = ticket_ref.get()
    if not ticket:
        raise HTTPException(status_code=404, detail="ไม่พบตั๋ว")

    slot_ref = get_slot_ref(slot_id)
    slot     = slot_ref.get()
    if not slot:
        raise HTTPException(status_code=404, detail=f"ไม่พบช่อง {slot_id}")
    if slot.get("status") == "occupied":
        raise HTTPException(status_code=400, detail=f"ช่อง {slot_id} ถูกใช้งานอยู่")

    # จอง slot
    slot_ref.update({"status": "reserved", "ticket_id": ticket_id})
    ticket_ref.update({"slot_id": slot_id})

    return {"success": True, "ticket_id": ticket_id, "slot_id": slot_id}


# ── init สร้าง 6 ช่องจอด (1-6) ──
@router.post("/slots/init")
def init_slots():
    """สร้างช่องจอด 1-6 ใน Firebase (ลบของเก่าทิ้งก่อน)"""
    slots_ref = get_slots_ref()
    slots_ref.delete()
    created = []
    SLOT_IDS = ["A1","A2","A3","A4","A5","A6"]
    for sid in SLOT_IDS:
        slots_ref.child(sid).set({
            "status":    "free",
            "ticket_id": None,
            "plate":     None,
            "time_in":   None
        })
        created.append(sid)
    return {"created": created, "total": len(created)}
