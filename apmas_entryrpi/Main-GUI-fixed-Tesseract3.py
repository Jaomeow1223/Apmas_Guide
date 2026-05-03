# -*- coding: utf-8 -*-
import tkinter as tk
from tkinter import Label
from PIL import Image, ImageTk

import cv2
import numpy as np
import onnxruntime as ort
import pytesseract
import subprocess
import threading
import uuid
import datetime
import time
import os
from collections import Counter

from rapidfuzz import fuzz, process
import firebase_admin
from firebase_admin import credentials, db as firebase_db
from gpiozero import Servo
from gpiozero.pins.pigpio import PiGPIOFactory
import requests

SERVER_URL = "http://10.153.161.199:8000"

TESSERACT_CMD       = "/usr/bin/tesseract"
LANGUAGE            = "tha"
PLATE_MODEL_PATH    = "/home/apmas99/Desktop/LicensePlate-EdgeAI/LicensePlate.onnx"
CODEPROV_MODEL_PATH = "/home/apmas99/Desktop/LicensePlate-EdgeAI/CodeProv.onnx"
THAI_PROVINCES_PATH = "/home/apmas99/Desktop/LicensePlate-EdgeAI/thai_provinces.txt"
CARLIST_PATH        = "/home/apmas99/Desktop/LicensePlate-EdgeAI/CarList.txt"
CAP_PATH            = "/home/apmas99/sandbox/cap.jpg"
IMAGE_SAVE_DIR      = "/home/apmas99/sandbox/captures/"

FIREBASE_CRED_PATH  = "/home/apmas99/Desktop/LicensePlate-EdgeAI/serviceAccountKey.json"
FIREBASE_DB_URL     = "https://apmas-parking-default-rtdb.asia-southeast1.firebasedatabase.app"
DEVICE_ID           = "entrance_pi4"

SERVO_PIN           = 18
SERVO_OPEN          = 1.0
SERVO_CLOSE         = -1.0
AUTO_CLOSE_SEC      = 10

CONF_THRESHOLD      = 0.10
IOU_THRESHOLD       = 0.45
IMG_SIZE            = 640

AUTO_DETECT_INTERVAL = 3
COOLDOWN_SEC         = 15

# OCR whitelist: เธเธขเธฑเธเธเธเธฐเนเธ—เธข + เธ•เธฑเธงเน€เธฅเธ เน€เธ—เนเธฒเธเธฑเนเธ (เธ•เธฑเธ”เธเธฑเธเธซเธงเธฑเธ”เธญเธญเธ)
PLATE_WHITELIST = (
    "\u0e01\u0e02\u0e03\u0e04\u0e05\u0e06\u0e07\u0e08\u0e09\u0e0a"
    "\u0e0b\u0e0c\u0e0d\u0e0e\u0e0f\u0e10\u0e11\u0e12\u0e13\u0e14"
    "\u0e15\u0e16\u0e17\u0e18\u0e19\u0e1a\u0e1b\u0e1c\u0e1d\u0e1e"
    "\u0e1f\u0e20\u0e21\u0e22\u0e23\u0e25\u0e27\u0e28\u0e29\u0e2a"
    "\u0e2b\u0e2c\u0e2d\u0e2e"
    "0123456789"
)

# ================================================================
os.makedirs(IMAGE_SAVE_DIR, exist_ok=True)
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

with open(THAI_PROVINCES_PATH, encoding="utf-8") as f:
    thai_provinces = [line.strip() for line in f if line.strip()]
with open(CARLIST_PATH, encoding="utf-8") as f:
    car_list = [line.strip().replace(" ", "") for line in f if line.strip()]

# Firebase
try:
    cred = credentials.Certificate(FIREBASE_CRED_PATH)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
    FIREBASE_OK = True
    print("[Firebase] Connection successful")
except Exception as e:
    FIREBASE_OK = False
    print(f"[Firebase] Cannot connect: {e}")

# Servo
try:
    factory  = PiGPIOFactory()
    servo    = Servo(SERVO_PIN, pin_factory=factory)
    servo.value = SERVO_CLOSE
    SERVO_OK = True
    print(f"[Servo] Ready GPIO{SERVO_PIN}")
except Exception as e:
    servo    = None
    SERVO_OK = False
    print(f"[Servo] Not ready: {e}")


# ================================================================
#  SERVO
# ================================================================
def open_gate_auto(ticket_id):
    if not SERVO_OK or servo is None:
        print("[Servo] skip (Not ready)")
        return
    def _run():
        servo.value = SERVO_OPEN
        print(f"[Servo] Open โ€” {ticket_id}")
        time.sleep(AUTO_CLOSE_SEC)
        servo.value = SERVO_CLOSE
        print(f"[Servo] Automatic close โ€” {ticket_id}")
    threading.Thread(target=_run, daemon=True).start()


def upload_image_to_server(local_path: str, ticket_id: str, label: str) -> str:
    try:
        with open(local_path, "rb") as f:
            res = requests.post(
                f"{SERVER_URL}/api/upload_image",
                files={"file": (f"{ticket_id}_{label}.jpg", f, "image/jpeg")},
                data={"ticket_id": ticket_id, "label": label},
                timeout=10
            )
        if res.status_code == 200:
            url = res.json().get("url", "")
            print(f"[Upload] {label} - {url}")
            return url
    except Exception as e:
        print(f"[Upload] Error {label}: {e}")
    return ""


# ================================================================
#  FIREBASE
# ================================================================
def send_to_firebase(ticket_id, plate_text, time_in, image_plate_url=None):
    if not FIREBASE_OK:
        return False
    try:
        firebase_db.reference(f"/tickets/{ticket_id}").set({
            "ticket_id":           ticket_id,
            "plate_text_raw":      plate_text,
            "plate_text_verified": None,
            "province_raw":        "",
            "province_verified":   None,
            "time_in":             time_in,
            "time_out":            None,
            "slot_id":             "pending",
            "status":              "pending",
            "image_car_url":       None,
            "image_plate_url":     image_plate_url,
            "qr_url":              None,
            "source":              DEVICE_ID
        })
        print(f"[Firebase] Send {ticket_id} OK")
        return True
    except Exception as e:
        print(f"[Firebase] Error: {e}")
        return False


def listen_for_qr_ready(ticket_id, on_ready_cb):
    """
    เธเธฑเธ Firebase เธฃเธญเธเธเธเธงเนเธฒ ticket เธเธฐเธกเธต qr_url (Pi5 เธเธฑเธ”เธเนเธญเธ + Server เธชเธฃเนเธฒเธ QR เนเธฅเนเธง)
    เธเธถเธเน€เธเธดเธ”เนเธกเนเธเธฑเนเธ โ€” เนเธกเนเธฃเธญ Admin verify เธญเธตเธเธ•เนเธญเนเธ
    """
    if not FIREBASE_OK:
        return
    ref = firebase_db.reference(f"/tickets/{ticket_id}")
    def _poll():
        for _ in range(180):   # เธฃเธญเธชเธนเธเธชเธธเธ” 3 เธเธฒเธ—เธต
            try:
                data    = ref.get() or {}
                qr_url  = data.get("qr_url")
                slot_id = data.get("slot_id", "")
                if qr_url and slot_id and slot_id not in ("Not assigned", "Not yet determined", "pending", ""):
                    on_ready_cb(ticket_id, slot_id, qr_url)
                    return
            except Exception as e:
                print(f"[Firebase] poll error: {e}")
            time.sleep(1)
        print(f"[Firebase] Timeout waiting QR for {ticket_id}")
    threading.Thread(target=_poll, daemon=True).start()


# ================================================================
#  MODEL UTILITIES
# ================================================================
def safe_crop(img, x1, y1, x2, y2):
    h, w = img.shape[:2]
    return img[max(0,y1):min(h,y2), max(0,x1):min(w,x2)]

def letterbox(im, new=640):
    h, w = im.shape[:2]
    r    = min(new/h, new/w)
    nh, nw = int(h*r), int(w*r)
    resized = cv2.resize(im, (nw, nh))
    canvas  = np.full((new, new, 3), 114, dtype=np.uint8)
    canvas[:nh, :nw] = resized
    return canvas, r

def nms(boxes, scores, iou_thres):
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

class YoloONNX:
    def __init__(self, path, mode="plate"):
        self.sess  = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self.iname = self.sess.get_inputs()[0].name
        self.mode  = mode
    def detect(self, img):
        lb, r = letterbox(img, IMG_SIZE)
        x = np.transpose(cv2.cvtColor(lb, cv2.COLOR_BGR2RGB).astype(np.float32)/255.0, (2,0,1))[None]
        out = np.squeeze(self.sess.run(None, {self.iname: x})[0])
        if out.shape[0] in (5,6): out = out.T
        boxes, scores, classes = [], [], []
        for row in out:
            if self.mode == "plate":
                if len(row)<5: continue
                cx,cy,w,h,conf=row[:5]; cls_id=0
            else:
                if len(row)<6: continue
                cx,cy,w,h=row[:4]
                if len(row)==6: conf=float(row[4]); cls_id=int(round(float(row[5])))
                else:
                    obj=float(row[4]); cs=row[5:]
                    cls_id=int(np.argmax(cs)); conf=obj*float(cs[cls_id])
            conf=float(conf)
            if conf<CONF_THRESHOLD: continue
            boxes.append([int((cx-w/2)/r),int((cy-h/2)/r),int((cx+w/2)/r),int((cy+h/2)/r)])
            scores.append(conf); classes.append(cls_id)
        if not boxes: return []
        keep = nms(boxes, scores, IOU_THRESHOLD)
        return [{"box":boxes[i],"class":classes[i],"conf":scores[i]} for i in keep]

plate_model    = YoloONNX(PLATE_MODEL_PATH, mode="plate")
codeprov_model = YoloONNX(CODEPROV_MODEL_PATH, mode="codeprov")


# ================================================================
#  STEP 1: PERSPECTIVE CORRECTION โ€” เนเธเนเธ เธฒเธเธเนเธฒเธขเน€เธญเธตเธขเธ
# ================================================================
def deskew_plate(img):
    """
    เธซเธฒ quadrilateral เธเธญเธเธเนเธฒเธข เนเธฅเนเธงเธ—เธณ perspective transform เนเธซเนเธ•เธฃเธ
    เธ–เนเธฒเธซเธฒ 4 เธกเธธเธกเนเธกเนเนเธ”เน เนเธเน minAreaRect เนเธ—เธ
    """
    gray   = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur   = cv2.GaussianBlur(gray, (5, 5), 0)
    _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    cnts, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return img

    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)
    quad = None
    for c in cnts[:5]:
        peri   = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.04 * peri, True)
        if len(approx) == 4:
            quad = approx.reshape(4, 2).astype(np.float32)
            break

    if quad is None:
        # fallback: minAreaRect
        rect = cv2.minAreaRect(cnts[0])
        quad = cv2.boxPoints(rect).astype(np.float32)

    # เน€เธฃเธตเธขเธเธเธธเธ”: top-left, top-right, bottom-right, bottom-left
    s      = quad.sum(axis=1)
    diff   = np.diff(quad, axis=1).flatten()
    pts    = np.zeros((4, 2), dtype=np.float32)
    pts[0] = quad[np.argmin(s)]       # top-left
    pts[2] = quad[np.argmax(s)]       # bottom-right
    pts[1] = quad[np.argmin(diff)]    # top-right
    pts[3] = quad[np.argmax(diff)]    # bottom-left

    W = int(max(np.linalg.norm(pts[1]-pts[0]), np.linalg.norm(pts[2]-pts[3])))
    H = int(max(np.linalg.norm(pts[3]-pts[0]), np.linalg.norm(pts[2]-pts[1])))
    if W < 10 or H < 10:
        return img

    dst = np.array([[0,0],[W-1,0],[W-1,H-1],[0,H-1]], dtype=np.float32)
    M   = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(img, M, (W, H))


# ================================================================
#  STEP 2: TIGHT CROP โ€” เธ•เธฑเธ”เธเธญเธเธญเธญเธ เนเธกเนเนเธซเน OCR เธ•เธดเธ”เธเธฃเธญเธเธเนเธฒเธข
# ================================================================
def tight_crop(img, pad=0.04):
    """
    เธซเธฒ bounding box เธเธญเธ content เธเธฃเธดเธเน เนเธฅเนเธงเธ•เธฑเธ”เธเธญเธเธญเธญเธ
    pad=0.04 = เน€เธเธทเนเธญเธเธญเธเนเธงเน 4% เธเธญเธเธเธเธฒเธ”เธ เธฒเธ (เธเธฃเธฑเธเนเธ”เน)
    """
    gray     = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, th    = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords   = cv2.findNonZero(th)
    if coords is None:
        return img
    x, y, w, h = cv2.boundingRect(coords)
    ih, iw      = img.shape[:2]
    px, py      = int(iw * pad), int(ih * pad)
    x1 = max(0,  x - px);     y1 = max(0,  y - py)
    x2 = min(iw, x + w + px); y2 = min(ih, y + h + py)
    out = img[y1:y2, x1:x2]
    return out if out.size > 0 else img


# ================================================================
#  STEP 3: OCR โ€” PaddleOCR + Tesseract fallback
# ================================================================

# เนเธซเธฅเธ” PaddleOCR เธเธฃเธฑเนเธเน€เธ”เธตเธขเธงเธ•เธญเธเน€เธฃเธดเนเธก (เนเธกเนเธ•เนเธญเธเนเธเน PyTorch)
try:
    from paddleocr import PaddleOCR
    _paddle = PaddleOCR(lang="thai", use_angle_cls=True, use_gpu=False, show_log=False)
    PADDLEOCR_OK = True
    print("[PaddleOCR] Loaded OK")
except Exception as e:
    _paddle = None
    PADDLEOCR_OK = False
    print(f"[PaddleOCR] Not available: {e} -- fallback to Tesseract")

def _preprocess_plate(gray):
    """เธเธขเธฒเธข โ’ CLAHE โ’ bilateral โ’ OTSU"""
    h = gray.shape[0]
    if h < 64:
        gray = cv2.resize(gray, None, fx=64/h, fy=64/h,
                          interpolation=cv2.INTER_CUBIC)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4,4))
    gray  = clahe.apply(gray)
    gray  = cv2.bilateralFilter(gray, 9, 75, 75)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary

def _filter_plate_text(text):
    return "".join(c for c in text if c in PLATE_WHITELIST)

def ocr_plate(img):
    """
    1. เธ•เธฑเธ”เธเธฃเธถเนเธเธเธ 65% (เธ•เธฑเธ”เธเธฑเธเธซเธงเธฑเธ”เธญเธญเธ)
    2. PaddleOCR (เนเธกเนเธ•เนเธญเธ PyTorch, เนเธกเนเธเธ เธฒเธฉเธฒเนเธ—เธข)
    3. fallback Tesseract PSM 7
    """
    h   = img.shape[0]
    top = img[:int(h * 0.65), :]
    if top.size == 0:
        top = img

    gray   = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
    binary = _preprocess_plate(gray)
    color  = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

    # โ”€โ”€ PaddleOCR โ”€โ”€
    if PADDLEOCR_OK and _paddle is not None:
        try:
            results = _paddle.ocr(color, cls=True)
            # results = [[ [[box], (text, conf)], ... ]]
            lines = results[0] if results and results[0] else []
            # เน€เธฃเธตเธขเธเธเนเธฒเธขโ’เธเธงเธฒเธ•เธฒเธก x เธเธญเธ bbox
            lines = sorted(lines, key=lambda r: r[0][0][0])
            texts = [_filter_plate_text(r[1][0]) for r in lines
                     if r[1][1] > 0.3 and len(_filter_plate_text(r[1][0])) >= 1]
            combined = "".join(texts)
            if len(combined) >= 3:
                print(f"[PaddleOCR] -> {repr(combined)}")
                return combined
        except Exception as e:
            print(f"[PaddleOCR] err: {e}")

    # โ”€โ”€ Tesseract fallback โ”€โ”€
    cfg = f"--psm 7 -c tessedit_char_whitelist={PLATE_WHITELIST} --oem 3"
    try:
        text    = pytesseract.image_to_string(binary, lang=LANGUAGE, config=cfg).strip()
        cleaned = _filter_plate_text(text)
        if len(cleaned) >= 3:
            print(f"[Tesseract] -> {repr(cleaned)}")
            return cleaned
    except Exception as e:
        print(f"[Tesseract] err: {e}")

    return ""


# ================================================================
#  CAPTURE & PIPELINE
# ================================================================
def capture_frame():
    subprocess.run(["rpicam-still", "-o", CAP_PATH,
                    "-t", "200", "--width", "1280", "--height", "720",
                    "--nopreview", "--immediate"], check=False)
    return cv2.imread(CAP_PATH)


def run_pipeline(frame):
    """
    เธเธทเธเธเนเธฒ (plate_img_final, display_frame, code_text)
    plate_img_final = เธ เธฒเธเธเนเธฒเธขเธ—เธตเนเนเธเนเน€เธญเธตเธขเธ+เธ•เธฑเธ”เธเธญเธเนเธฅเนเธง
    display_frame   = เธ เธฒเธเน€เธ•เนเธกเธเธฃเนเธญเธก bounding box
    code_text       = เธ•เธฑเธงเธญเธฑเธเธฉเธฃ+เน€เธฅเธเธ—เธตเนเธญเนเธฒเธเนเธ”เน
    """
    if frame is None:
        return None, None, ""

    plates = plate_model.detect(frame)
    if not plates:
        return None, frame, ""

    best          = max(plates, key=lambda x: x["conf"])
    x1, y1, x2, y2 = best["box"]

    # เธงเธฒเธ” bounding box เธเธเธ เธฒเธเน€เธ•เนเธก
    disp = frame.copy()
    cv2.rectangle(disp, (x1,y1), (x2,y2), (0,255,100), 3)
    cv2.putText(disp, f"conf:{best['conf']:.2f}",
                (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,100), 2)

    # crop เธ”เธดเธ
    plate_raw = safe_crop(frame, x1, y1, x2, y2)
    if plate_raw is None or plate_raw.size == 0:
        return None, disp, ""

    # โ”€โ”€ 3 เธเธฑเนเธเธ•เธญเธเธซเธฅเธฑเธ โ”€โ”€
    plate_deskewed = deskew_plate(plate_raw)          # เนเธเนเน€เธญเธตเธขเธ
    plate_tight    = tight_crop(plate_deskewed, 0.04) # เธ•เธฑเธ”เธเธญเธ
    code_text      = ocr_plate(plate_tight)           # OCR

    return plate_tight, disp, code_text


# ================================================================
#  GUI
# ================================================================
root = tk.Tk()
root.configure(bg="#0d0d0d")
root.title("APMAS - Entrance (Auto)")

auto_running   = False
last_sent_time = 0

# Header
hdr = tk.Frame(root, bg="#111827")
hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
tk.Label(hdr, text="APMAS  ENTRANCE", fg="#00e5a0", bg="#111827",
         font=("Courier",16,"bold")).pack(side="left", padx=16, pady=10)
tk.Label(hdr,
         text=f"Firebase {'OK' if FIREBASE_OK else 'ERR'}   Servo {'OK' if SERVO_OK else 'ERR'}",
         fg="lime" if (FIREBASE_OK and SERVO_OK) else "orange",
         bg="#111827", font=("Courier",11)).pack(side="right", padx=16)

# Image preview
default_img = Image.new("RGB", (640,480), "#111")
photo       = ImageTk.PhotoImage(default_img)
image_label = Label(root, image=photo, bg="#0d0d0d")
image_label.grid(row=1, column=0, rowspan=10, padx=12, pady=8)

def row_lbl(r, txt, color="skyblue"):
    tk.Label(root, text=txt, fg=color, bg="#0d0d0d",
             font=("Prompt",14,"bold")).grid(row=r, column=1, sticky="w", padx=8)

def row_val(r, default="---", w=26):
    v = tk.Label(root, text=default, fg="white", bg="#1e2530",
                 font=("Prompt",14), width=w, anchor="w")
    v.grid(row=r, column=1, sticky="w", padx=8, pady=2)
    return v

row_lbl(1, "License plate (OCR)")
plate_value  = row_val(2)
row_lbl(3, "Status", "#ff6b35")
status_value = row_val(4, w=32)
row_lbl(5, "Ticket ID")
ticket_value = tk.Label(root, text="---", fg="#ffd23f", bg="#0d0d0d",
                         font=("Courier",13), width=26, anchor="w")
ticket_value.grid(row=6, column=1, sticky="w", padx=8)

auto_indicator = tk.Label(root, text="Stop working", fg="#5a6478",
                           bg="#0d0d0d", font=("Courier",11))
auto_indicator.grid(row=7, column=1, sticky="w", padx=8, pady=2)


# ================================================================
#  AUTO-DETECT LOOP
# ================================================================
def auto_detect_loop():
    global last_sent_time
    while auto_running:
        now = time.time()

        # cooldown เธซเธฅเธฑเธเธชเนเธเธชเธณเน€เธฃเนเธ
        if now - last_sent_time < COOLDOWN_SEC:
            rem = int(COOLDOWN_SEC - (now - last_sent_time))
            root.after(0, lambda r=rem: auto_indicator.config(
                text=f"cooldown {r}s", fg="#ffd23f"))
            time.sleep(1)
            continue

        root.after(0, lambda: auto_indicator.config(text="Detecting...", fg="#00e5a0"))
        root.after(0, lambda: status_value.config(text="Detecting...", fg="#5a6478"))

        frame = capture_frame()
        plate_img, disp, code_text = run_pipeline(frame)

        # เนเธชเธ”เธเธ เธฒเธเน€เธ•เนเธกเธเธฃเนเธญเธก bounding box
        if disp is not None:
            rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            ph  = ImageTk.PhotoImage(Image.fromarray(rgb).resize((640,480)))
            root.after(0, lambda p=ph: _set_img(p))

        if not code_text:
            root.after(0, lambda: status_value.config(
                text="License plate not found, scanning...", fg="#5a6478"))
            root.after(0, lambda: auto_indicator.config(
                text=f"Check every {AUTO_DETECT_INTERVAL}s", fg="#5a6478"))
            time.sleep(AUTO_DETECT_INTERVAL)
            continue

        # โ”€โ”€ เธเธเธเนเธฒเธข โ”€โ”€
        ticket_id = "TK" + uuid.uuid4().hex[:8].upper()
        time_in   = datetime.datetime.now().isoformat()

        # เนเธชเธ”เธเธ เธฒเธเธเนเธฒเธขเธ—เธตเนเนเธเนเน€เธญเธตเธขเธเนเธฅเนเธง
        if plate_img is not None:
            rgb_c = cv2.cvtColor(plate_img, cv2.COLOR_BGR2RGB)
            ph_c  = ImageTk.PhotoImage(Image.fromarray(rgb_c).resize((640,480)))
            root.after(0, lambda p=ph_c: _set_img(p))

        root.after(0, lambda c=code_text: plate_value.config(text=c))
        root.after(0, lambda t=ticket_id: ticket_value.config(text=t))
        root.after(0, lambda: status_value.config(
            text="เธชเนเธเธเนเธญเธกเธนเธฅเนเธฅเนเธง โ€” เธฃเธญ Pi5 เธเธฑเธ”เธเนเธญเธ + QR...", fg="orange"))

        # เธญเธฑเธเนเธซเธฅเธ”เธฃเธนเธเธเนเธฒเธข
        img_plate_url = ""
        if plate_img is not None:
            img_plate_path = os.path.join(IMAGE_SAVE_DIR, f"{ticket_id}_plate.jpg")
            cv2.imwrite(img_plate_path, plate_img)
            img_plate_url = upload_image_to_server(img_plate_path, ticket_id, "plate")

        ok = send_to_firebase(ticket_id, code_text, time_in, img_plate_url or None)
        if ok:
            last_sent_time = time.time()
            def on_qr_ready(tid, slot_id, qr_url):
                open_gate_auto(tid)
                root.after(0, lambda s=slot_id: status_value.config(
                    text=f"เธเนเธญเธ {s} โ€” เน€เธเธดเธ”เนเธกเนเธเธฑเนเธ! ({AUTO_CLOSE_SEC}s)", fg="lime"))
                root.after(0, lambda: auto_indicator.config(
                    text=f"Opened - cooldown {COOLDOWN_SEC}s", fg="lime"))
            listen_for_qr_ready(ticket_id, on_qr_ready)
        else:
            root.after(0, lambda: status_value.config(
                text="Cannot send to Firebase", fg="red"))

        root.after(0, lambda: auto_indicator.config(
            text=f"cooldown {COOLDOWN_SEC}s", fg="#ffd23f"))
        time.sleep(AUTO_DETECT_INTERVAL)

    root.after(0, lambda: auto_indicator.config(text="Stop working", fg="#5a6478"))


def _set_img(ph):
    image_label.config(image=ph)
    image_label.image = ph


# Buttons
bf = tk.Frame(root, bg="#0d0d0d")
bf.grid(row=8, column=1, sticky="we", padx=8, pady=10)

def start_auto():
    global auto_running
    if auto_running: return
    auto_running = True
    btn_start.config(state="disabled", bg="#444")
    btn_stop.config(state="normal", bg="#e53935")
    threading.Thread(target=auto_detect_loop, daemon=True).start()

def stop_auto():
    global auto_running
    auto_running = False
    btn_start.config(state="normal", bg="#1a73e8")
    btn_stop.config(state="disabled", bg="#444")

btn_start = tk.Button(bf, text="Start automatic detection",
    command=start_auto, font=("Arial",13,"bold"),
    bg="#1a73e8", fg="white", relief="flat", padx=10, pady=8)
btn_start.pack(side="left", fill="x", expand=True, padx=(0,4))

btn_stop = tk.Button(bf, text="Stop",
    command=stop_auto, font=("Arial",13,"bold"),
    bg="#444", fg="white", relief="flat", padx=10, pady=8, state="disabled")
btn_stop.pack(side="left", fill="x", expand=True, padx=(4,0))

root.mainloop()
