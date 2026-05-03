from fastapi import APIRouter, HTTPException
from database import get_tickets_ref, get_ticket_ref, get_slot_ref, get_slots_ref
from models import VerifyPayload
from utils.qr_generator import generate_qr
import datetime, os, threading, time

router = APIRouter()
SERVER_BASE_URL = "http://10.153.161.199:8000"

@router.get("/tickets/all")
def get_all_tickets():
    data = get_tickets_ref().get()
    if not data: return []
    tickets = list(data.values())
    tickets.sort(key=lambda x: x.get("time_in",""), reverse=True)
    return tickets[:100]

@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    data = get_ticket_ref(ticket_id).get()
    if not data:
        raise HTTPException(status_code=404, detail="ไม่พบตั๋ว")
    t_in  = data.get("time_in")
    t_out = data.get("time_out")
    try:
        if t_in and not t_out:
            delta = datetime.datetime.now() - datetime.datetime.fromisoformat(t_in)
        elif t_in and t_out:
            delta = datetime.datetime.fromisoformat(t_out) - datetime.datetime.fromisoformat(t_in)
        else:
            delta = None
    except:
        delta = None
    if delta:
        total = int(delta.total_seconds())
        h, r  = divmod(total, 3600)
        m, s  = divmod(r, 60)
        data["duration"] = f"{h:02d}:{m:02d}:{s:02d}"
    else:
        data["duration"] = "--:--:--"
    return data

# ── Admin ยืนยัน (Pi5 จะเป็นคนตัดสินใจช่อง) ──
@router.patch("/tickets/{ticket_id}/verify")
def verify_ticket(ticket_id: str, payload: VerifyPayload):
    ref  = get_ticket_ref(ticket_id)
    data = ref.get()
    if not data:
        raise HTTPException(status_code=404, detail="ไม่พบตั๋ว")

    # อัปเดต ticket → status=verified (Pi5 จะฟัง Firebase แล้วตัดสินใจช่อง)
    ref.update({
        "plate_text_verified": payload.plate_text_verified,
        "province_verified":   None,
        "slot_id":             "pending",   # Pi5 จะมาอัปเดต
        "status":              "verified",
    })

    return {
        "success":             True,
        "ticket_id":           ticket_id,
        "plate_text_verified": payload.plate_text_verified,
        "slot_id":             "pending",
        "message":             "ยืนยันแล้ว — รอ Pi5 จัดช่องจอด"
    }

# ── Pi5 แจ้ง slot_id ที่จัดให้ ticket (พร้อมสร้าง QR) ──
@router.patch("/tickets/{ticket_id}/assign_slot")
def assign_slot_from_pi5(ticket_id: str, slot_id: str):
    ref  = get_ticket_ref(ticket_id)
    data = ref.get()
    if not data:
        raise HTTPException(status_code=404, detail="ไม่พบตั๋ว")

    # จอง slot ใน Firebase
    get_slot_ref(slot_id).update({
        "status":    "reserved",
        "ticket_id": ticket_id
    })

    # สร้าง QR พร้อม slot_id ใน URL
    qr_link     = f"https://jaomeow1.github.io/apmas/?ticket_id={ticket_id}&slot={slot_id}"
    qr_path     = generate_qr(ticket_id, qr_link)
    qr_filename = os.path.basename(qr_path)
    qr_url      = f"{SERVER_BASE_URL}/images/{qr_filename}"

    # อัปเดต ticket ด้วย slot_id + qr_url
    ref.update({
        "slot_id": slot_id,
        "qr_url":  qr_url
    })

    return {
        "success":   True,
        "ticket_id": ticket_id,
        "slot_id":   slot_id,
        "qr_url":    qr_url,
        "message":   f"จัดช่อง {slot_id} สำเร็จ — QR พร้อมแล้ว"
    }

@router.patch("/tickets/{ticket_id}/checkout")
def checkout_ticket(ticket_id: str):
    ref  = get_ticket_ref(ticket_id)
    data = ref.get()
    if not data:
        raise HTTPException(status_code=404, detail="ไม่พบตั๋ว")
    time_out = datetime.datetime.now().isoformat()
    ref.update({"time_out": time_out, "status": "exited"})
    return {"success": True, "time_out": time_out}


# ── Firebase background listener — สร้าง QR เมื่อ Pi5 assign slot แล้ว ──
_qr_created = set()   # ป้องกัน สร้าง QR ซ้ำ

def _watch_slot_assigned():
    """
    Server รัน background thread นี้
    ฟัง /tickets realtime → เมื่อ status=slot_assigned → สร้าง QR ทันที
    """
    from database import get_tickets_ref, get_ticket_ref
    from utils.qr_generator import generate_qr
    import os
    try:
        import firebase_admin
        from firebase_admin import db as _fdb
        ref = _fdb.reference("/tickets")

        def _on_change(event):
            try:
                data = ref.get() or {}
                for tid, t in data.items():
                    if (t.get("status") == "slot_assigned"
                            and t.get("slot_id")
                            and tid not in _qr_created):
                        _qr_created.add(tid)
                        slot_id = t["slot_id"]
                        plate   = t.get("plate_text_verified") or t.get("plate_text_raw") or ""
                        print(f"[Server] 📦 สร้าง QR ticket={tid} slot={slot_id}")

                        # สร้าง QR
                        qr_link = (f"https://jaomeow1.github.io/apmas/"
                                   f"?ticket_id={tid}&slot={slot_id}")
                        qr_path    = generate_qr(tid, qr_link)
                        qr_url     = f"{SERVER_BASE_URL}/images/{os.path.basename(qr_path)}"

                        # อัปเดต Firebase: qr_url + status=verified
                        get_ticket_ref(tid).update({
                            "qr_url": qr_url,
                            "status": "verified",
                        })
                        print(f"[Server] ✅ QR ready {tid} → {slot_id}")
            except Exception as e:
                print(f"[Server] watcher error: {e}")

        ref.listen(_on_change)
    except Exception as e:
        print(f"[Server] _watch_slot_assigned init error: {e}")

def start_slot_watcher():
    t = threading.Thread(target=_watch_slot_assigned, daemon=True)
    t.start()
    print("[Server] slot_watcher started")
