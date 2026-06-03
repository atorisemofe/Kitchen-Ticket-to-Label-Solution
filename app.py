from datetime import datetime, timezone, timedelta
import os
import base64
import json
import uuid
import requests
import jwt
import threading
import time
from flask import Flask, redirect, session, request, render_template, url_for
from dotenv import load_dotenv
from smcs_oauth_client import SMCSOAuthClient
from llm_parser import parse_receipt_with_openai

load_dotenv()

app = Flask(__name__)
app.secret_key = "super-secret-key"

# =========================
# ENV CONFIG
# =========================

CLIENT_ID = os.getenv("SMCS_CLIENT_ID")
DEVICE_ID = os.getenv("SMCS_DEVICE_ID")
AUTH_URL = os.getenv("AUTH_URL")
TOKEN_URL = os.getenv("TOKEN_URL")
SCOPES = os.getenv("SCOPES").split(",")
TOKEN_FILE = os.getenv("SMCS_TOKEN_FILE")

OPENWEBUI_URL = os.getenv("OPENWEBUI_URL")
OPENWEBUI_TOKEN = os.getenv("OPENWEBUI_TOKEN")
MODEL_NAME = os.getenv("LLM_MODEL_NAME")

STAR_API_KEY = os.getenv("STAR_API_KEY")
STAR_HOST = os.getenv("STAR_HOST")
GROUP_PATH = os.getenv("STAR_GROUP_PATH")

SETTINGS_FILE = os.getenv("SETTINGS_FILE")
LAST_RECEIPTS_FILE = os.getenv("LAST_RECEIPT_FILE")
PRINTED_RECEIPTS_FILE = os.getenv("PRINTED_RECEIPTS_FILE")

# =========================
# Helper Functions
# =========================

def get_smcs_client():
    return SMCSOAuthClient(
        client_id=CLIENT_ID,
        auth_url=AUTH_URL,
        token_url=TOKEN_URL,
        scopes=SCOPES,
        token_file=TOKEN_FILE
    )

def save_settings(data):
    os.makedirs(".tokens", exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {}

def load_last_receipts():
    if os.path.exists(LAST_RECEIPTS_FILE):
        with open(LAST_RECEIPTS_FILE) as f:
            return json.load(f)
    return []


def load_printed_receipts():
    if not PRINTED_RECEIPTS_FILE:
        return []

    if os.path.exists(PRINTED_RECEIPTS_FILE):
        with open(PRINTED_RECEIPTS_FILE, "r") as f:
            return json.load(f)

    return []

def save_printed_receipts(receipts):
    os.makedirs(os.path.dirname(PRINTED_RECEIPTS_FILE), exist_ok=True)

    # Keep last 20 for safety
    with open(PRINTED_RECEIPTS_FILE, "w") as f:
        json.dump(receipts[-20:], f)


def get_printer_image(model_name):
    if not model_name:
        return "/static/images/printers/mC-Print3.webp"

    model = model_name.lower()

    if "mclabel2" in model or "mc-label2" in model:
        return "/static/images/printers/mC-Label2.webp"

    if "mclabel3" in model or "mc-label3" in model:
        return "/static/images/printers/mC-Label3.webp"

    if "mcprint3" in model or "mc-print3" in model:
        return "/static/images/printers/mC-Print3.webp"

    if "mcprint2" in model or "mc-print2" in model:
        return "/static/images/printers/mC-Print2.webp"

    if "tsp100iv" in model:
        return "/static/images/printers/TSP100IV.webp"

    # Default fallback
    return "/static/images/printers/mC-Print3.webp"

# ====================================================
# LLM PARSING
# ====================================================

def parse_receipt_with_llm(image_path, retries=3):

    for attempt in range(retries):

        try:
            with open(image_path, "rb") as f:
                image_bytes = f.read()

            base64_image = base64.b64encode(image_bytes).decode("utf-8")

            prompt = """
                    Extract structured food orders from this receipt.
                    Return ONLY valid JSON in this format:

                    [
                    {
                        "customer": "Tom",
                        "order_number": "123",
                        "pickup_time": "6:45 PM",
                        "quantity": 1,
                        "item": "Basket Boneless Wings",
                        "modifiers": ["Buffalo", "Bleu Cheese"]
                    }
                    ]

                    Do not include totals or addresses.
                    If quantity missing assume 1.
                    Return JSON only.
                    """

            headers = {
                "Authorization": f"Bearer {OPENWEBUI_TOKEN}",
                "Content-Type": "application/json"
            }

            data = {
                "model": MODEL_NAME,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
            }

            response = requests.post(OPENWEBUI_URL, headers=headers, json=data)

            if response.status_code != 200:
                raise Exception(f"LLM Error: {response.text}")

            content = response.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        
        except Exception as e:
            print(f"LLM attempt {attempt+1} failed:", e)
            time.sleep(2 ** attempt)
    
    raise Exception("LLM failed after retries")

# ====================================================
# STAR MARKUP
# ====================================================

def generate_star_markup(items):

    total_items = len(items)
    document = ""

    for index, item in enumerate(items, start=1):

        document += "[align: center]\n"
        document += "[negative: on]\n"
        document += "[magnify: width 4; height 3]\n"
        document += f"  {item.get('customer','').upper()}  \n"
        document += "[negative: off]\n"

        document += "[align: left]\n"
        document += "[magnify: width 2; height 2]\n"
        document += f"[column: left Order # {item.get("order_number", "")}; right #{index} of {total_items}]\n"

        pickup = item.get("pickup_time", "")
        document += f"Pick up time {pickup}\n"

        document += "[bold: on]\n"
        # document += "[magnify: width 2; height 2]\n"
        document += f"{item['item']}\n"
        document += "[bold: off]\n"

        for mod in item.get("modifiers", []):
            document += f"-----{mod}\n"

        # document += "[align: right]\n"
        # document += f"{item.get('customer','').upper()}\n"

        document += "[plain]\n"
        document += "[cut: feed; partial]\n\n"

    return document

# ====================================================
# PRINT TO STAR
# ====================================================

def send_print_job(markup_text, kitchen_printer_id):

    job_name = f"Kitchen-Label-{uuid.uuid4().hex[:8]}"

    url = f"{STAR_HOST}/a/{GROUP_PATH}/d/{kitchen_printer_id}/q?copies=1"

    headers = {
        "Content-Type": "text/vnd.star.markup",
        "Star-Api-Key": STAR_API_KEY,
        "Star-Job-Name": job_name
    }

    response = requests.post(url, headers=headers, data=markup_text.encode("utf-8"))

    if response.status_code not in (201, 202):
        raise Exception(f"StarIO Error: {response.text}")

    return response.json()

auto_print_lock = threading.Lock()

def check_and_auto_print():

    if not auto_print_lock.acquire(blocking=False):
        return

    try:

        settings = load_settings()
        receipt_printer_id = settings.get("receipt_printer")
        kitchen_printer_id = settings.get("kitchen_printer")

        if not receipt_printer_id or not kitchen_printer_id:
            return

        # smcs_client = get_smcs_client()
        # token = smcs_client.get_access_token()
        # ✅ Always create fresh client to reload tokens
        smcs_client = SMCSOAuthClient(
            client_id=CLIENT_ID,
            auth_url=AUTH_URL,
            token_url=TOKEN_URL,
            scopes=SCOPES,
            token_file=TOKEN_FILE
        )

        try:
            token = smcs_client.get_access_token()
        except Exception as e:
            print("Auto print skipped:", e)
            return

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        printed_receipts = load_printed_receipts()

        # ✅ Determine restart recovery point
        if printed_receipts:
            last_uploaded = printed_receipts[-1]["uploaded"]
        else:
            last_uploaded = None

        params = {"data_type": "url"}

        if last_uploaded:
            # params["from"] = last_uploaded
            last_dt = datetime.fromisoformat(last_uploaded.replace("Z", "+00:00"))
            last_dt += timedelta(milliseconds=1)
            params["from"] = last_dt.isoformat()

        params["to"] = datetime.now(timezone.utc).isoformat()

        response = requests.get(
            f"https://device-manager.smcs.io/printer/api/v1/devices/{receipt_printer_id}/receipts",
            headers=headers,
            params=params
        )

        if response.status_code != 200:
            return

        receipts = response.json()

        receipts.sort(key=lambda x: x.get("uploaded") or "")

        for receipt in receipts:

            receipt_id = receipt.get("id")
            uploaded = receipt.get("uploaded")

            if not receipt_id or not uploaded:
                continue

            # ✅ Skip already printed
            if any(r["id"] == receipt_id for r in printed_receipts):
                continue

            image_url = receipt.get("uri")

            if not image_url:
                continue

            image_response = requests.get(image_url, headers=headers)

            if not image_response.ok:
                continue

            image_path = f".tokens/receipts/{receipt_id}.png"
            os.makedirs(".tokens/receipts", exist_ok=True)

            with open(image_path, "wb") as f:
                f.write(image_response.content)

            # # ✅ Parse & print
            # parsed_items = parse_receipt_with_llm(image_path)
            # markup = generate_star_markup(parsed_items)

            # result = send_print_job(markup, kitchen_printer_id)

            # # ✅ Store metadata
            # printed_receipts.append({
            #     "receipt_printer_id": receipt_printer_id,
            #     "id": receipt_id,
            #     "uploaded": uploaded,
            #     "printed_at": datetime.utcnow().isoformat() + "Z",
            #     "job_id": result.get("JobId")
            # })

            # save_printed_receipts(printed_receipts)
            # ✅ Immediately mark as processing
            printed_receipts.append({
                "receipt_printer_id": receipt_printer_id,
                "id": receipt_id,
                "uploaded": uploaded,
                "printed_at": None,
                "job_id": None,
                "status": "processing"
            })

            save_printed_receipts(printed_receipts)

            # ✅ Now do slow work
            # parsed_items = parse_receipt_with_llm(image_path)
            try:
                parsed_items = parse_receipt_with_openai(image_path)
            except Exception as e:
                print("LLM parsing failed:", e)
                return
            markup = generate_star_markup(parsed_items)
            result = send_print_job(markup, kitchen_printer_id)

            # ✅ Update the entry
            for r in printed_receipts:
                if r["id"] == receipt_id:
                    r["printed_at"] = datetime.utcnow().isoformat() + "Z"
                    r["job_id"] = result.get("JobId")
                    r["status"] = "printed"
                    break

            save_printed_receipts(printed_receipts)
        
    finally:
        auto_print_lock.release()

# =========================
# Routes
# =========================

@app.route("/")
def index():

    if "authenticated" not in session:
        return redirect(url_for("login"))
    
    smcs_client = get_smcs_client()

    try:
        # ✅ Validate token
        smcs_client.get_access_token()
    except Exception:
        # ✅ Token invalid or expired
        session.clear()
        return redirect(url_for("login"))

    # printed_receipts = load_printed_receipts()

    # latest_two = printed_receipts[-2:][::-1]
    printed_receipts = load_printed_receipts()

    settings = load_settings()
    receipt_printer_id = settings.get("receipt_printer")

    # ✅ Only show receipts for currently selected printer
    filtered = [
        r for r in printed_receipts
        if r.get("receipt_printer_id") == receipt_printer_id
    ]

    latest_two = filtered[-2:][::-1]

    receipts = []

    for r in latest_two:
        receipts.append({
            "id": r["id"],
            "uploaded": r["uploaded"],
            "printed_at": r["printed_at"],
            "image_path": f"/receipt_image/{r['id']}"
        })

    return render_template("index.html", receipts=receipts)

@app.route("/api/latest_receipts")
def api_latest_receipts():

    # printed_receipts = load_printed_receipts()

    # latest_two = printed_receipts[-2:][::-1]
    printed_receipts = load_printed_receipts()

    settings = load_settings()
    receipt_printer_id = settings.get("receipt_printer")

    filtered = [
        r for r in printed_receipts
        if r.get("receipt_printer_id") == receipt_printer_id
    ]

    latest_two = filtered[-2:][::-1]

    receipts = []

    for r in latest_two:
        receipts.append({
            "id": r["id"],
            "uploaded": r["uploaded"],
            "printed_at": r["printed_at"],
            "image_path": f"/receipt_image/{r['id']}"
        })

    return {"receipts": receipts}

@app.route("/receipt_image/<receipt_id>")
def receipt_image(receipt_id):
    file_path = f".tokens/receipts/{receipt_id}.png"

    if os.path.exists(file_path):
        return open(file_path, "rb").read(), 200, {
            "Content-Type": "image/png"
        }

    return "Not Found", 404


@app.route("/login")
def login():

    smcs_client = get_smcs_client()

    try:
        smcs_client.authenticate()   # ✅ explicitly start OAuth flow
        session["authenticated"] = True
    except Exception as e:
        return f"Login failed: {e}"

    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/print_receipt/<receipt_id>", methods=["POST"])
def print_receipt(receipt_id):

    settings = load_settings()
    kitchen_printer_id = settings.get("kitchen_printer")

    if not kitchen_printer_id:
        return {
            "status": "error",
            "message": "Kitchen printer not configured"
        }, 400

    image_path = f".tokens/receipts/{receipt_id}.png"

    if not os.path.exists(image_path):
        return {
            "status": "error",
            "message": "Receipt image not found"
        }, 404

    try:
        # Step 1: Parse
        parsed_items = parse_receipt_with_llm(image_path)

        # Step 2: Generate Markup
        markup = generate_star_markup(parsed_items)

        # Step 3: Send to StarIO
        result = send_print_job(markup, kitchen_printer_id)

        return {
            "status": "success",
            "job_id": result.get("JobId"),
            "job_name": result.get("Name")
        }, 200

    except Exception as e:
        print("Print error:", e)
        return {
            "status": "error",
            "message": str(e)
        }, 500

@app.route("/settings", methods=["GET", "POST"])
def settings():

    smcs_client = get_smcs_client()

    # =========================
    # HANDLE SAVE
    # =========================
    if request.method == "POST":

        receipt_printer = request.form.get("receipt_printer")
        kitchen_printer = request.form.get("kitchen_printer")

        save_settings({
            "receipt_printer": receipt_printer,
            "kitchen_printer": kitchen_printer
        })

        # return redirect(url_for("index"))
        return redirect(url_for("settings", saved_success="1"))


    # =========================
    # LOAD SAVED SETTINGS
    # =========================
    success = request.args.get("saved_success")
    saved = load_settings() or {}

    # =========================
    # GET SMCS DEVICES
    # =========================
    receipt_devices = []

    try:
        token = smcs_client.get_access_token()
        user_name = "Unknown User"

        try:
            decoded = jwt.decode(token, options={"verify_signature": False})
            user_name = decoded.get("name") or decoded.get("preferred_username")
        except Exception as e:
            print("JWT decode error:", e)

        smcs_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        smcs_response = requests.get(
            "https://device-manager.smcs.io/printer/api/v1/devices",
            headers=smcs_headers
        )

        if smcs_response.status_code == 200:

            smcs_devices = smcs_response.json()

            receipt_devices = [
                {
                    "device_id": d.get("device_id"),
                    "device_name": d.get("device_name"),
                    "product_type": d.get("product_type"),
                    "serial_number": d.get("serial_number"),
                    # "image_url": d.get("image_url"),
                    "image_url": get_printer_image(d.get("product_type")),
                    "active": d.get("active"),
                    "owned": d.get("owned"),
                }
                for d in smcs_devices
            ]

        else:
            print("SMCS Devices API Error:",
                  smcs_response.status_code,
                  smcs_response.text)

    except Exception as e:
        print("SMCS Exception:", e)

    # =========================
    # GET STARIO DEVICES
    # =========================
    kitchen_devices = []

    try:
        star_headers = {
            "Content-Type": "application/json",
            "Star-Api-Key": STAR_API_KEY
        }

        star_response = requests.get(
            f"{STAR_HOST}/a/{GROUP_PATH}/d",
            headers=star_headers
        )

        if star_response.status_code == 200:

            star_devices = star_response.json()

            kitchen_devices = [
            {
                "AccessIdentifier": d.get("AccessIdentifier"),
                "ClientType": d.get("ClientType"),
                "SerialNumber": d.get("SerialNumber"),
                "image_url": get_printer_image(d.get("ClientType")),
                "Online": d.get("Status", {}).get("Online"),
                "PaperEmpty": d.get("Status", {}).get("PaperEmpty"),
                "PaperLow": d.get("Status", {}).get("PaperLow"),
            }
            for d in star_devices
            if d.get("AccessIdentifier")
        ]

        else:
            print("StarIO Devices API Error:",
                  star_response.status_code,
                  star_response.text)

    except Exception as e:
        print("StarIO Exception:", e)

    # Determine selected receipt printer details
    selected_receipt = None
    if saved.get("receipt_printer"):
        for d in receipt_devices:
            if d["device_id"] == saved.get("receipt_printer"):
                selected_receipt = d
                break

    # Determine selected kitchen printer details
    selected_kitchen = None
    if saved.get("kitchen_printer"):
        for d in kitchen_devices:
            if d["AccessIdentifier"] == saved.get("kitchen_printer"):
                selected_kitchen = d
                break

    # =========================
    # RENDER PAGE
    # =========================
    return render_template(
        "settings.html",
        receipt_devices=receipt_devices,
        kitchen_devices=kitchen_devices,
        saved=saved,
        selected_receipt=selected_receipt,
        selected_kitchen=selected_kitchen,
        success=success,
        user_name=user_name
    )

def start_auto_print_loop():

    def loop():
        delay = 10
        while True:
            try:
                check_and_auto_print()
                delay = 10  # reset on success
            except Exception as e:
                print("Auto print error:", e)
                delay = min(delay * 2, 60)  # exponential backoff
            time.sleep(delay)

    thread = threading.Thread(target=loop)
    thread.daemon = True
    thread.start()

if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_auto_print_loop()
    app.run(debug=True)