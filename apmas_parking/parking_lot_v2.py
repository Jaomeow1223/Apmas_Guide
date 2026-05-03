# -*- coding: utf-8 -*-

import lgpio, time, threading, subprocess, requests, re
import tkinter as tk
from tkinter import Label, Frame
from PIL import Image, ImageTk
import cv2, numpy as np, pytesseract
from collections import Counter
import firebase_admin
from firebase_admin import credentials, db as firebase_db

try:
    import onnxruntime as ort
    ORT_OK = True
except ImportError:
    ORT_OK = False
    print("[onnxruntime] not installed — pip install onnxruntime --break-system-packages")

# =========================================================
# CONFIG
# =========================================================

SERVER_URL         = "http://10.153.161.199:8000"
FIREBASE_CRED_PATH = "/home/apmas98/project/serviceAccountKey.json"
FIREBASE_DB_URL    = "https://apmas-parking-default-rtdb.asia-southeast1.firebasedatabase.app"
CAP_PATH           = "/home/apmas98/project/capture.jpg"

pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

PLATE_MODEL_PATH = "/home/apmas98/project/LicensePlate.onnx"

CONF_THRESHOLD = 0.10
IOU_THRESHOLD  = 0.45
IMG_SIZE       = 640
LANGUAGE       = "tha"

PLATE_WHITELIST = (
    "\u0e01\u0e02\u0e03\u0e04\u0e05\u0e06\u0e07\u0e08\u0e09\u0e0a"
    "\u0e0b\u0e0c\u0e0d\u0e0e\u0e0f\u0e10\u0e11\u0e12\u0e13\u0e14"
    "\u0e15\u0e16\u0e17\u0e18\u0e19\u0e1a\u0e1b\u0e1c\u0e1d\u0e1e"
    "\u0e1f\u0e20\u0e21\u0e22\u0e23\u0e25\u0e27\u0e28\u0e29\u0e2a"
    "\u0e2b\u0e2c\u0e2d\u0e2e0123456789"
)

# =========================================================
# SLOT SENSOR (6 ช่อง)
# =========================================================

SLOT_SENSORS = {
    "A1": {"trig": 23, "echo": 24},
    "A2": {"trig": 16, "echo": 25},
    "A3": {"trig": 5,  "echo": 19},
    "A4": {"trig": 12, "echo": 13},
    "A5": {"trig": 6,  "echo": 26},
    "A6": {"trig": 20, "echo": 21},
}

ALL_SLOTS = ["A1", "A2", "A3", "A4", "A5", "A6"]

# =========================================================
# BUZZER (6 ตัว)
# =========================================================

SLOT_BUZZER = {
    "A1": 27,
    "A2": 17,
    "A3": 22,
    "A4": 10,
    "A5":  9,
    "A6": 11,
}

# =========================================================
# LED เขียว (6 ตัว) — ติด=ว่าง, ดับ=มีรถ
# =========================================================

SLOT_LED = {
    "A1": 2,
    "A2": 3,
    "A3": 4,
    "A4": 14,
    "A5": 15,
    "A6": 18,
}

DETECT_CM      = 15
CLEAR_CM       = 50
EXIT_RESET_SEC = 15   # รอ 15 วิหลังรถออก ก่อนตัดสินใจ reset
COOLDOWN       = 15

# =========================================================
# GPIO INIT
# =========================================================

h = lgpio.gpiochip_open(0)

for pin in SLOT_BUZZER.values():
    lgpio.gpio_claim_output(h, pin)

# LED เขียว — init เป็น output แล้วเปิดทุกตัว (ทุกช่องว่าง)
for pin in SLOT_LED.values():
    lgpio.gpio_claim_output(h, pin)
    lgpio.gpio_write(h, pin, 1)   # ติดทันที (Active HIGH)

for cfg in SLOT_SENSORS.values():
    lgpio.gpio_claim_output(h, cfg["trig"])
    lgpio.gpio_claim_input(h,  cfg["echo"])

# slot_state:
#   occupied     — sensor เห็นรถอยู่
#   last_time    — timestamp รถเข้าล่าสุด (cooldown)
#   plate_checked — OCR เสร็จแล้ว ไม่ต้องทำซ้ำ
#   matched      — match กับ ticket สำเร็จแล้ว (ใช้ตัดสินใจ reset)
slot_state = {
    sid: {
        "occupied":      False,
        "last_time":     0,
        "plate_checked": False,
        "matched":       False,
        "exit_gen":      0,     # เพิ่มทุกครั้งที่รถออก — ใช้ยกเลิก _delayed_reset เก่า
    }
    for sid in SLOT_SENSORS
}

# =========================================================
# LED HELPERS
# =========================================================

def led_on(slot):
    """ไฟเขียวติด — ช่องว่าง"""
    pin = SLOT_LED.get(slot)
    if pin is not None:
        try:
            lgpio.gpio_write(h, pin, 1)
        except Exception as e:
            print(f"[LED] led_on {slot} error: {e}")

def led_off(slot):
    """ไฟเขียวดับ — มีรถ"""
    pin = SLOT_LED.get(slot)
    if pin is not None:
        try:
            lgpio.gpio_write(h, pin, 0)
        except Exception as e:
            print(f"[LED] led_off {slot} error: {e}")

# =========================================================
# FIREBASE INIT
# =========================================================

try:
    cred = credentials.Certificate(FIREBASE_CRED_PATH)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
    FIREBASE_OK = True
    print("[Firebase] Connected OK")
except Exception as e:
    FIREBASE_OK = False
    print(f"[Firebase] Error: {e}")

assigned_tickets = set()
assign_lock      = threading.Lock()

# =========================================================
# SLOT SORT KEY
# =========================================================

def slot_sort_key(sid):
    m = re.match(r'^([A-Za-z]*)(\d+)$', sid)
    return (m.group(1), int(m.group(2))) if m else (sid, 0)

# =========================================================
# ตัดสินใจช่องจอด
# =========================================================

def pick_free_slot():
    try:
        slots_data = firebase_db.reference("/slots").get() or {}
        for sid in sorted(slots_data.keys(), key=slot_sort_key):
            if slots_data[sid].get("status") == "free":
                return sid
    except Exception as e:
        print(f"[Pi5] pick_free_slot error: {e}")
    return None

def assign_slot_to_ticket(ticket_id, plate_text):
    with assign_lock:
        if ticket_id in assigned_tickets:
            return
        assigned_tickets.add(ticket_id)

    slot_id = pick_free_slot()
    if not slot_id:
        log(f"[Pi5] ไม่มีช่องว่างสำหรับ {ticket_id}")
        return

    log(f"[Pi5] \U0001f17f จัดช่อง {slot_id} → {ticket_id} ({plate_text})")

    try:
        firebase_db.reference(f"/slots/{slot_id}").update({
            "status":    "reserved",
            "ticket_id": ticket_id,
            "plate":     plate_text,
        })
        log(f"[Firebase] /slots/{slot_id} = reserved ✓")
    except Exception as e:
        log(f"[Firebase] write slots error: {e}")

    try:
        firebase_db.reference(f"/tickets/{ticket_id}").update({
            "slot_id": slot_id,
            "status":  "slot_assigned",
        })
        log(f"[Firebase] /tickets/{ticket_id} slot_id={slot_id} status=slot_assigned ✓")
    except Exception as e:
        log(f"[Firebase] write ticket error: {e}")

    try:
        r = requests.patch(
            f"{SERVER_URL}/api/tickets/{ticket_id}/assign_slot",
            params={"slot_id": slot_id},
            timeout=5
        )
        data = r.json()
        log(f"[Server] {data.get('message', 'assign_slot OK')}")
    except Exception as e:
        log(f"[Server] assign_slot unreachable: {e}")

# =========================================================
# FIREBASE LISTENERS
# =========================================================

def listen_tickets():
    if not FIREBASE_OK:
        return

    def on_change(event):
        try:
            tickets_data = firebase_db.reference("/tickets").get() or {}
            for tid, t in tickets_data.items():
                slot_val = t.get("slot_id") or ""
                is_unassigned = slot_val in ("pending", "Not assigned",
                                             "Not yet determined", "")
                if (t.get("status") == "pending"
                        and is_unassigned
                        and tid not in assigned_tickets):
                    plate = t.get("plate_text_raw") or ""
                    log(f"[Pi5] \U0001f698 ticket ใหม่ {tid} ป้าย '{plate}'")
                    threading.Thread(
                        target=assign_slot_to_ticket,
                        args=(tid, plate),
                        daemon=True
                    ).start()
        except Exception as e:
            print(f"[Firebase] ticket listener error: {e}")

    firebase_db.reference("/tickets").listen(on_change)

def listen_slots():
    if not FIREBASE_OK:
        return

    def on_change(event):
        try:
            data = firebase_db.reference("/slots").get() or {}
            root.after(0, lambda d=dict(data): refresh_slot_ui_from_firebase(d))
        except Exception as e:
            print(f"[Firebase] slot listener error: {e}")

    firebase_db.reference("/slots").listen(on_change)

def refresh_slot_ui_from_firebase(slots_data):
    for sid, sv in slots_data.items():
        if sid in slot_widgets:
            status = sv.get("status", "free")
            _update_card(
                sid,
                sv.get("plate") or "--",
                status,
                sv.get("ticket_id") or "",
            )
            # LED ควบคุมจาก sensor เป็นหลัก
            # Firebase sync แค่กรณี occupied จริงๆ เท่านั้นที่ดับ
            # reserved/free/checking → LED ติด (sensor จะดับเองเมื่อรถเข้า)
            if status == "occupied":
                led_off(sid)
            elif status in ("free",):
                # ดับแค่เมื่อ sensor ยังไม่เห็นรถ (ป้องกัน Firebase override sensor)
                if not slot_state.get(sid, {}).get("occupied", False):
                    led_on(sid)

# =========================================================
# BUZZER — เหลือแค่ beep_err (ไม่ match)
# =========================================================

def beep_err(slot):
    buz = SLOT_BUZZER[slot]
    for _ in range(3):
        lgpio.tx_pwm(h, buz, 1200, 50); time.sleep(0.25)
        lgpio.tx_pwm(h, buz, 1200, 0);  time.sleep(0.10)

# =========================================================
# SENSOR
# =========================================================

def get_distance(trig, echo):
    lgpio.gpio_write(h, trig, 0)
    time.sleep(0.005)
    lgpio.gpio_write(h, trig, 1)
    time.sleep(0.00001)
    lgpio.gpio_write(h, trig, 0)
    timeout = time.time() + 0.1
    while lgpio.gpio_read(h, echo) == 0:
        if time.time() > timeout: return 999
    start = time.time()
    timeout = time.time() + 0.1
    while lgpio.gpio_read(h, echo) == 1:
        if time.time() > timeout: return 999
    return (time.time() - start) * 17150

# =========================================================
# YOLO ONNX
# =========================================================

def _letterbox(im, new=640):
    h_, w = im.shape[:2]
    r    = min(new/h_, new/w)
    nh, nw = int(h_*r), int(w*r)
    resized = cv2.resize(im, (nw, nh))
    canvas  = np.full((new, new, 3), 114, dtype=np.uint8)
    canvas[:nh, :nw] = resized
    return canvas, r

def _nms(boxes, scores, iou_thres):
    idxs = np.argsort(scores)[::-1]; keep = []
    while len(idxs) > 0:
        i = idxs[0]; keep.append(i); rest = idxs[1:]; new_idxs = []
        for j in rest:
            xx1=max(boxes[i][0],boxes[j][0]); yy1=max(boxes[i][1],boxes[j][1])
            xx2=min(boxes[i][2],boxes[j][2]); yy2=min(boxes[i][3],boxes[j][3])
            inter=max(0,xx2-xx1)*max(0,yy2-yy1)
            ai=max(0,boxes[i][2]-boxes[i][0])*max(0,boxes[i][3]-boxes[i][1])
            aj=max(0,boxes[j][2]-boxes[j][0])*max(0,boxes[j][3]-boxes[j][1])
            if inter/(ai+aj-inter+1e-9) < iou_thres: new_idxs.append(j)
        idxs = np.array(new_idxs, dtype=np.int64)
    return keep

def _safe_crop(img, x1, y1, x2, y2):
    fh, fw = img.shape[:2]
    return img[max(0,y1):min(fh,y2), max(0,x1):min(fw,x2)]

class YoloONNX:
    def __init__(self, path, mode="plate"):
        self.sess  = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self.iname = self.sess.get_inputs()[0].name
        self.mode  = mode
    def detect(self, img):
        lb, r = _letterbox(img, IMG_SIZE)
        x = np.transpose(
            cv2.cvtColor(lb, cv2.COLOR_BGR2RGB).astype(np.float32)/255.0,
            (2,0,1))[None]
        out = np.squeeze(self.sess.run(None, {self.iname: x})[0])
        if out.shape[0] in (5,6): out = out.T
        boxes, scores, classes = [], [], []
        for row in out:
            if self.mode == "plate":
                if len(row)<5: continue
                cx,cy,w,h_,conf=row[:5]; cls_id=0
            else:
                if len(row)<6: continue
                cx,cy,w,h_=row[:4]
                if len(row)==6: conf=float(row[4]); cls_id=int(round(float(row[5])))
                else:
                    obj=float(row[4]); cs=row[5:]
                    cls_id=int(np.argmax(cs)); conf=obj*float(cs[cls_id])
            conf=float(conf)
            if conf<CONF_THRESHOLD: continue
            boxes.append([int((cx-w/2)/r),int((cy-h_/2)/r),
                          int((cx+w/2)/r),int((cy+h_/2)/r)])
            scores.append(conf); classes.append(cls_id)
        if not boxes: return []
        keep = _nms(boxes, scores, IOU_THRESHOLD)
        return [{"box":boxes[i],"class":classes[i],"conf":scores[i]} for i in keep]

if ORT_OK:
    try:
        plate_model = YoloONNX(PLATE_MODEL_PATH, mode="plate")
        YOLO_OK = True
        print("[YOLO] plate_model OK")
    except Exception as e:
        plate_model = None
        YOLO_OK = False
        print(f"[YOLO] Not available: {e} — fallback to fixed crop")
else:
    plate_model = None
    YOLO_OK = False
    print("[YOLO] onnxruntime not installed — fallback to fixed crop")

# =========================================================
# PaddleOCR
# =========================================================

try:
    from paddleocr import PaddleOCR
    _paddle = None
    for kwargs in [
        {"lang": "th", "use_textline_orientation": True},
        {"lang": "th", "use_angle_cls": True},
        {"lang": "th"},
    ]:
        try:
            _paddle = PaddleOCR(**kwargs)
            print(f"[PaddleOCR] Loaded OK with {list(kwargs.keys())}")
            break
        except TypeError:
            continue
    if _paddle is None:
        raise RuntimeError("ไม่สามารถสร้าง PaddleOCR ได้เลย")
    PADDLEOCR_OK = True
except Exception as e:
    _paddle = None
    PADDLEOCR_OK = False
    print(f"[PaddleOCR] Not available: {e} — fallback to Tesseract")

# =========================================================
# OCR PIPELINE
# =========================================================

def deskew_plate(img):
    gray   = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur   = cv2.GaussianBlur(gray, (5, 5), 0)
    _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts: return img
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)
    quad = None
    for c in cnts[:5]:
        peri   = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.04*peri, True)
        if len(approx) == 4:
            quad = approx.reshape(4,2).astype(np.float32); break
    if quad is None:
        rect = cv2.minAreaRect(cnts[0])
        quad = cv2.boxPoints(rect).astype(np.float32)
    s      = quad.sum(axis=1); diff = np.diff(quad, axis=1).flatten()
    pts    = np.zeros((4,2), dtype=np.float32)
    pts[0] = quad[np.argmin(s)];    pts[2] = quad[np.argmax(s)]
    pts[1] = quad[np.argmin(diff)]; pts[3] = quad[np.argmax(diff)]
    W = int(max(np.linalg.norm(pts[1]-pts[0]), np.linalg.norm(pts[2]-pts[3])))
    H = int(max(np.linalg.norm(pts[3]-pts[0]), np.linalg.norm(pts[2]-pts[1])))
    if W < 10 or H < 10: return img
    dst = np.array([[0,0],[W-1,0],[W-1,H-1],[0,H-1]], dtype=np.float32)
    M   = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(img, M, (W, H))

def tight_crop(img, pad=0.04):
    gray     = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, th    = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords   = cv2.findNonZero(th)
    if coords is None: return img
    x, y, w, h_ = cv2.boundingRect(coords)
    ih, iw      = img.shape[:2]
    px, py      = int(iw*pad), int(ih*pad)
    x1=max(0,x-px);     y1=max(0,y-py)
    x2=min(iw,x+w+px);  y2=min(ih,y+h_+py)
    out = img[y1:y2, x1:x2]
    return out if out.size > 0 else img

def _preprocess_plate(gray):
    if gray.shape[0] < 64:
        gray = cv2.resize(gray, None, fx=64/gray.shape[0], fy=64/gray.shape[0],
                          interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4,4))
    gray  = clahe.apply(gray)
    gray  = cv2.bilateralFilter(gray, 9, 75, 75)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary

def _filter_plate_text(text):
    return "".join(c for c in text if c in PLATE_WHITELIST)

def ocr_plate(img):
    top = img[:int(img.shape[0]*0.65), :]
    if top.size == 0: top = img
    gray   = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
    binary = _preprocess_plate(gray)
    color  = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

    if PADDLEOCR_OK and _paddle is not None:
        try:
            results = _paddle.ocr(color, cls=True)
            lines   = results[0] if results and results[0] else []
            lines   = sorted(lines, key=lambda r: r[0][0][0])
            texts   = [_filter_plate_text(r[1][0]) for r in lines
                       if r[1][1] > 0.3 and len(_filter_plate_text(r[1][0])) >= 1]
            combined = "".join(texts)
            if len(combined) >= 3:
                print(f"[PaddleOCR] {repr(combined)}")
                return combined
        except Exception as e:
            print(f"[PaddleOCR] err: {e}")

    cfg  = f"--psm 7 -c tessedit_char_whitelist={PLATE_WHITELIST} --oem 3"
    try:
        text = pytesseract.image_to_string(binary, lang=LANGUAGE, config=cfg).strip()
        out  = _filter_plate_text(text)
        if len(out) >= 3:
            print(f"[Tesseract] {repr(out)}")
            return out
    except Exception as e:
        print(f"[Tesseract] err: {e}")
    return ""

def run_pipeline(frame):
    if frame is None: return None, None, ""
    disp = frame.copy()
    if YOLO_OK and plate_model is not None:
        plates = plate_model.detect(frame)
        if plates:
            best         = max(plates, key=lambda x: x["conf"])
            x1,y1,x2,y2 = best["box"]
            cv2.rectangle(disp, (x1,y1), (x2,y2), (0,255,100), 3)
            plate_raw = _safe_crop(frame, x1, y1, x2, y2)
        else:
            fh, fw    = frame.shape[:2]
            plate_raw = frame[int(fh*0.35):int(fh*0.75), int(fw*0.2):int(fw*0.8)]
    else:
        fh, fw    = frame.shape[:2]
        plate_raw = frame[int(fh*0.35):int(fh*0.75), int(fw*0.2):int(fw*0.8)]

    if plate_raw is None or plate_raw.size == 0:
        return None, disp, ""

    plate_deskewed = deskew_plate(plate_raw)
    plate_tight    = tight_crop(plate_deskewed, 0.04)
    text           = ocr_plate(plate_tight)
    return plate_tight, disp, text

def ocr_vote(frame, n=3):
    results = []
    for _ in range(n):
        f = capture_frame()
        if f is None: continue
        _, _, text = run_pipeline(f)
        if len(text) >= 3:
            results.append(text)
    if not results: return ""
    best    = Counter(results).most_common(1)[0][0]
    longest = max(results, key=len)
    return longest if len(longest) > len(best) + 1 else best

# =========================================================
# CAMERA
# =========================================================

def capture_frame():
    subprocess.run([
        "rpicam-still", "-o", CAP_PATH,
        "-t", "1500",
        "--width", "1280", "--height", "720",
        "--autofocus-mode", "auto",
        "--nopreview"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cv2.imread(CAP_PATH)

def camera_loop():
    while True:
        frame = capture_frame()
        if frame is not None:
            _, disp, _ = run_pipeline(frame)
            show = disp if disp is not None else frame
            rgb  = cv2.cvtColor(show, cv2.COLOR_BGR2RGB)
            img  = Image.fromarray(rgb).resize((640, 360))
            root.after(0, lambda i=img: set_preview(ImageTk.PhotoImage(i)))
        time.sleep(3)

# =========================================================
# SERVER
# =========================================================

def report_occupied(slot, plate):
    try:
        r = requests.patch(
            f"{SERVER_URL}/api/slots/{slot}",
            json={"status": "occupied", "plate_text": plate},
            timeout=8
        )
        data = r.json()
        log(f"[Server] HTTP {r.status_code} | {data}")
        return data
    except Exception as e:
        log(f"[Server] report_occupied error: {e}")
        return {"match": False}

def report_free(slot):
    try:
        r = requests.patch(
            f"{SERVER_URL}/api/slots/{slot}",
            json={"status": "free"},
            timeout=8
        )
        log(f"[Server] {slot} ว่างแล้ว (HTTP {r.status_code})")
    except Exception as e:
        log(f"[Server] report_free error: {e} → fallback Firebase")
        try:
            firebase_db.reference(f"/slots/{slot}").update({
                "status": "free", "ticket_id": None,
                "plate":  None,   "time_in":   None,
            })
        except Exception as e2:
            log(f"[Firebase] report_free error: {e2}")

# =========================================================
# PROCESS SLOT
# =========================================================

_active_slots  = set()
_active_lock   = threading.Lock()
_exiting_slots = set()
_exit_lock     = threading.Lock()

def process_slot(slot):
    """
    OCR pipeline — ป้องกัน thread ซ้อน
    Logic การ match/ไม่ match:
      - ระหว่าง attempt: ถ้ารถออก (occupied=False) → abort ทันที ไม่เปลี่ยน GUI
      - match ✅ → set matched=True, อัปเดต GUI เป็น occupied (ไม่มีเสียง)
      - ไม่ match ❌ ตลอด max_retry → beep_err + แสดง ERR แต่ไม่ reset Firebase
    """
    with _active_lock:
        if slot in _active_slots:
            return
        _active_slots.add(slot)

    try:
        root.after(0, lambda s=slot: _update_card(s, "...", "checking", ""))
        max_retry = 8

        for attempt in range(max_retry):
            # รถออกก่อน match → abort เงียบๆ ไม่เปลี่ยน GUI
            if not slot_state[slot]["occupied"]:
                log(f"[{slot}] รถออกก่อน match — abort (GUI คงเดิม)")
                return

            frame = capture_frame()
            if frame is None:
                time.sleep(0.5); continue

            plate_text = ocr_vote(frame, n=3)
            log(f"[{slot}] attempt {attempt+1} OCR='{plate_text}'")

            if len(plate_text) < 3:
                time.sleep(0.8); continue

            root.after(0, lambda s=slot, p=plate_text: _update_card(s, p, "checking", ""))

            plate_img, _, _ = run_pipeline(frame)
            if plate_img is not None:
                rgb = cv2.cvtColor(plate_img, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb).resize((640, 300))
                root.after(0, lambda i=img: set_preview(ImageTk.PhotoImage(i)))

            result = report_occupied(slot, plate_text)

            if result.get("match"):
                # ✅ match — ไม่มีเสียง, อัปเดต GUI เท่านั้น
                slot_state[slot]["plate_checked"] = True
                slot_state[slot]["matched"]       = True
                tid = result.get("ticket_id", "")
                root.after(0, lambda s=slot, p=plate_text, t=tid:
                           _update_card(s, p, "occupied", t))
                log(f"[{slot}] ✅ จอดสำเร็จ {plate_text}")
                return

            else:
                # ❌ ยังไม่ match — beep_err เท่านั้น
                if slot_state[slot]["occupied"]:
                    threading.Thread(target=beep_err, args=(slot,), daemon=True).start()
                    log(f"[{slot}] ❌ ไม่ตรง {plate_text}")
                    time.sleep(1.5)
                else:
                    # รถออกระหว่างรอ → abort เงียบๆ
                    log(f"[{slot}] รถออกระหว่าง retry — abort")
                    return

        log(f"[{slot}] หมด {max_retry} ครั้ง — ยกเลิก (GUI คงเดิม)")
        root.after(0, lambda s=slot: _update_card(s, "ERR", "err", ""))

    finally:
        with _active_lock:
            _active_slots.discard(slot)

# =========================================================
# DELAYED RESET — เรียกหลังรถออก 15 วินาที
# =========================================================

def _delayed_reset(slot, gen):
    """
    รอ EXIT_RESET_SEC วิ แล้วตัดสินใจ:
    - ถ้ารถคันใหม่เข้าก่อนครบ 15 วิ → exit_gen จะเปลี่ยน → ยกเลิกทันที

    matched=True  → รถจอดถูกต้องแล้วออกไป
                    → mark ticket=exited, reset slot, อัปเดต GUI

    matched=False → รถยังไม่ match หรือออกก่อน match
                    → ไม่ reset GUI/Firebase อะไรทั้งนั้น
    """
    time.sleep(EXIT_RESET_SEC)

    # รถคันใหม่เข้ามาแล้ว → gen เปลี่ยน → ยกเลิก reset นี้
    if slot_state[slot]["exit_gen"] != gen:
        log(f"[{slot}] _delayed_reset ยกเลิก (รถใหม่เข้าก่อนครบ {EXIT_RESET_SEC}s)")
        with _exit_lock:
            _exiting_slots.discard(slot)
        return

    was_matched = slot_state[slot].get("matched", False)

    if was_matched:
        # ── รถ match แล้วออกไป → reset ทุกอย่าง ──
        log(f"[{slot}] 🚪 matched → reset หลัง {EXIT_RESET_SEC}s")

        # mark ticket exited ใน Firebase
        try:
            import datetime
            all_tickets = firebase_db.reference("/tickets").get() or {}
            for tid, t in all_tickets.items():
                if (t.get("slot_id") == slot and
                        t.get("status") in ("parked", "slot_assigned", "verified")):
                    firebase_db.reference(f"/tickets/{tid}").update({
                        "status":   "exited",
                        "time_out": datetime.datetime.now().isoformat(),
                    })
                    log(f"[{slot}] ticket {tid} → exited")
        except Exception as e:
            log(f"[{slot}] Firebase exit error: {e}")

        # reset state
        slot_state[slot]["plate_checked"] = False
        slot_state[slot]["matched"]       = False

        # อัปเดต UI + Server (LED ติดคืนตอนรถออกจากระยะ sensor แล้ว)
        root.after(0, lambda s=slot: _update_card(s, "--", "free", ""))
        report_free(slot)
        log(f"[{slot}] ✓ reset เรียบร้อย")

    else:
        # ── ยังไม่ match → ไม่ทำอะไร GUI/Firebase คงเดิม ──
        slot_state[slot]["plate_checked"] = False
        slot_state[slot]["matched"]       = False
        log(f"[{slot}] ↩ ยังไม่ match — GUI/Firebase คงเดิม พร้อมรับรถใหม่")

    with _exit_lock:
        _exiting_slots.discard(slot)

# =========================================================
# SENSOR WORKER — แยก thread ต่อช่อง
# =========================================================

def _sensor_worker(slot, cfg):
    """
    วัด sensor อิสระทุก 0.3s ไม่รอช่องอื่น
    รถเข้า → LED ดับ
    รถออก  → ไม่เปลี่ยน GUI ทันที — รอ _delayed_reset ตัดสินใจ (LED ติดคืนใน _delayed_reset)
    """
    log(f"[Sensor] {slot} started (trig={cfg['trig']} echo={cfg['echo']})")
    while True:
        try:
            dist  = get_distance(cfg["trig"], cfg["echo"])
            state = slot_state[slot]
            now   = time.time()

            # รถเข้า
            if (dist < DETECT_CM
                    and not state["occupied"]
                    and not state["plate_checked"]):
                if now - state["last_time"] > COOLDOWN:
                    state["occupied"]  = True
                    state["last_time"] = now
                    led_off(slot)   # ← ไฟเขียวดับเมื่อรถเข้า
                    log(f"[{slot}] \U0001f697 รถเข้า {dist:.0f}cm — LED ดับ")
                    threading.Thread(target=process_slot,
                                     args=(slot,), daemon=True).start()

            # รถออก
            elif dist > CLEAR_CM and state["occupied"]:
                with _exit_lock:
                    if slot in _exiting_slots:
                        pass  # _delayed_reset กำลังนับอยู่แล้ว
                    else:
                        _exiting_slots.add(slot)
                        state["occupied"] = False
                        state["exit_gen"] += 1          # เพิ่ม gen ทุกครั้งที่รถออก
                        current_gen = state["exit_gen"]
                        led_on(slot)    # ← LED ติดทันทีที่รถพ้นระยะ sensor
                        log(f"[{slot}] ↩ รถออก {dist:.0f}cm — LED ติด, gen={current_gen}, ตรวจสอบใน {EXIT_RESET_SEC}s")
                        threading.Thread(
                            target=_delayed_reset,
                            args=(slot, current_gen), daemon=True
                        ).start()

        except Exception as e:
            log(f"[{slot}] sensor error: {e}")

        time.sleep(0.3)

def sensor_loop():
    for slot, cfg in SLOT_SENSORS.items():
        threading.Thread(
            target=_sensor_worker,
            args=(slot, cfg),
            daemon=True,
            name=f"sensor-{slot}"
        ).start()
    log(f"[Sensor] {len(SLOT_SENSORS)} threads started: {', '.join(SLOT_SENSORS.keys())}")

# =========================================================
# GUI
# =========================================================

root = tk.Tk()
root.title("APMAS Parking Lot")
root.geometry("1200x700")
root.configure(bg="#0d0d0d")

header = tk.Frame(root, bg="#111827")
header.pack(fill="x")
Label(header, text="APMAS PARKING LOT",
      fg="#00ffaa", bg="#111827",
      font=("Courier", 16, "bold")).pack(side="left", padx=20, pady=10)
Label(header, text=f"Server: {SERVER_URL}",
      fg="#374151", bg="#111827",
      font=("Courier", 9)).pack(side="right", padx=14)
Label(header,
      text=(f"Firebase {'OK' if FIREBASE_OK else 'OFF'} | "
            f"YOLO {'OK' if YOLO_OK else 'fallback'} | "
            f"OCR {'Paddle+Tesseract' if PADDLEOCR_OK else 'Tesseract'}"),
      fg="lime" if FIREBASE_OK else "orange",
      bg="#111827").pack(side="right", padx=20)

preview_img   = ImageTk.PhotoImage(Image.new("RGB", (640, 360), "#111"))
preview_label = Label(root, image=preview_img, bg="#0d0d0d")
preview_label.pack(pady=10)

def set_preview(ph):
    preview_label.config(image=ph)
    preview_label.image = ph

slot_frame  = tk.Frame(root, bg="#0d0d0d")
slot_frame.pack()
slot_widgets = {}

S_COLOR = {
    "free":     "#00ffaa",
    "reserved": "#f5c842",
    "occupied": "#ff4444",
    "err":      "#ff4444",
    "checking": "#ffaa00",
}
S_TEXT = {
    "free":     "ว่าง",
    "reserved": "จอง",
    "occupied": "ไม่ว่าง",
    "err":      "ผิดช่อง",
    "checking": "กำลังตรวจ",
}

for col, sid in enumerate(ALL_SLOTS):
    has_sensor = sid in SLOT_SENSORS
    card = tk.Frame(slot_frame, bg="#1e2530", padx=20, pady=15)
    card.grid(row=0, column=col, padx=10)

    Label(card, text=f"ช่อง {sid}",
          fg="white", bg="#1e2530",
          font=("Courier", 14)).pack()
    Label(card,
          text="● sensor" if has_sensor else "○ no sensor",
          fg="#00ffaa" if has_sensor else "#374151",
          bg="#1e2530", font=("Courier", 8)).pack()

    plate_lbl = Label(card, text="--",
                      fg="white", bg="#1e2530",
                      font=("Courier", 20))
    plate_lbl.pack()

    status_lbl = Label(card, text="ว่าง",
                       fg="#00ffaa", bg="#1e2530")
    status_lbl.pack()

    tid_lbl = Label(card, text="",
                    fg="#374151", bg="#1e2530",
                    font=("Courier", 8))
    tid_lbl.pack()

    slot_widgets[sid] = {
        "frame":      card,
        "plate_lbl":  plate_lbl,
        "status_lbl": status_lbl,
        "tid_lbl":    tid_lbl,
    }

def _update_card(slot, plate, status, tid=""):
    if slot not in slot_widgets:
        return
    w  = slot_widgets[slot]
    bg = {"occupied": "#1a0a0a", "err": "#1a0a0a",
          "reserved": "#1a1500"}.get(status, "#1e2530")
    w["plate_lbl"].config(text=plate or "--")
    w["status_lbl"].config(
        text=S_TEXT.get(status, status),
        fg=S_COLOR.get(status, "white")
    )
    w["tid_lbl"].config(text=tid[:16] if tid else "")
    w["frame"].config(bg=bg)
    for child in w["frame"].winfo_children():
        child.config(bg=bg)

log_frame = tk.Frame(root, bg="#0d0d0d")
log_frame.pack(fill="x", padx=20, pady=(0, 8))
log_box = tk.Text(log_frame, height=4, bg="#111827", fg="#4b5563",
                  font=("Courier", 8), relief="flat",
                  state="disabled", wrap="word")
log_box.pack(fill="x")

def log(msg):
    log_box.config(state="normal")
    log_box.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    log_box.see("end")
    log_box.config(state="disabled")
    print(msg)

# =========================================================
# START
# =========================================================

def on_start():
    log("▶ เริ่มระบบ APMAS Parking Lot (Pi5)")
    log(f"  ตัดสินใจช่องจอด : Pi5 (A1 → A2 → ... → A6)")
    log(f"  Server           : {SERVER_URL}")
    log(f"  Firebase         : {'OK' if FIREBASE_OK else 'ERROR'}")
    log(f"  LED เขียว        : {list(SLOT_LED.items())}")

    if FIREBASE_OK:
        try:
            init_data = firebase_db.reference("/slots").get() or {}
            refresh_slot_ui_from_firebase(init_data)
            log(f"  Slots ใน Firebase: {list(init_data.keys())}")
            # ซิงค์ LED กับ Firebase — ช่อง occupied → LED ดับ
            for sid, sv in init_data.items():
                if sv.get("status") in ("occupied", "reserved", "checking"):
                    led_off(sid)
                else:
                    led_on(sid)
        except Exception as e:
            log(f"  Firebase read error: {e}")

    threading.Thread(target=sensor_loop,    daemon=True).start()
    threading.Thread(target=listen_slots,   daemon=True).start()
    threading.Thread(target=listen_tickets, daemon=True).start()
    threading.Thread(target=camera_loop,    daemon=True).start()
    log("  Ready ✓")

def on_close():
    print("[System] Closing GPIO...")
    try:
        for pin in SLOT_BUZZER.values():
            try: lgpio.tx_pwm(h, pin, 1000, 0)
            except: pass
        # ปิด LED ทุกตัวก่อน close
        for pin in SLOT_LED.values():
            try: lgpio.gpio_write(h, pin, 0)
            except: pass
        lgpio.gpiochip_close(h)
        print("[System] GPIO closed OK")
    except Exception as e:
        print(f"[System] GPIO close error: {e}")
    root.destroy()

root.protocol("WM_DELETE_WINDOW", on_close)

import signal
signal.signal(signal.SIGTERM, lambda *_: on_close())
signal.signal(signal.SIGINT,  lambda *_: on_close())

root.after(600, on_start)
root.mainloop()
