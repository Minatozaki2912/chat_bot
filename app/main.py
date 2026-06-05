import json
import os
import uuid
import requests
import time
import io
import base64
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from datetime import datetime
import app.config as config
import app.database as database
import app.prompts as prompts

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Thư mục 1: Lưu ảnh AI design nguyên gốc
os.makedirs("design_outputs", exist_ok=True)
app.mount("/design_outputs", StaticFiles(directory="design_outputs"), name="design_outputs")

# Thư mục 2: Lưu ảnh Mockup tự động
os.makedirs("mockups_outputs", exist_ok=True)
app.mount("/mockups_outputs", StaticFiles(directory="mockups_outputs"), name="mockups_outputs")
# ==================== PROJECT ROOT ====================
import pathlib

_PROJECT_ROOT = str(pathlib.Path(__file__).parent.parent.absolute())
print(f"✅ PROJECT_ROOT initialized: {_PROJECT_ROOT}")


# =====================================================================
# HÀM MÃ HÓA ẢNH NỘI BỘ THÀNH BASE64 (ĐỂ AI ĐỌC ĐƯỢC)
# =====================================================================
def get_valid_image_url(image_path_or_url):
    """
    Biến đổi link nội bộ (/design_outputs/...) thành chuỗi Base64.
    Nếu đã là base64 sẵn từ Frontend, hoặc là link web ngoài thì giữ nguyên.
    """
    if image_path_or_url and image_path_or_url.startswith("/design_outputs/"):
        local_file = image_path_or_url.lstrip("/")
        try:
            with open(local_file, "rb") as f:
                b64_str = base64.b64encode(f.read()).decode("utf-8")
                # Đổi thành image/jpeg vì bây giờ lưu file JPG
                return f"data:image/jpeg;base64,{b64_str}"
        except Exception as e:
            print(f"Lỗi đọc file nội bộ: {e}")
    return image_path_or_url


# =====================================================================
# LOAD SỔ TAY RAG (CHÍNH SÁCH BÁN HÀNG)
# =====================================================================
KNOWLEDGE_FILE = "app/knowledge.json"
KNOWLEDGE_DATA = ""
try:
    with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
        KNOWLEDGE_DATA = f.read()
except Exception as e:
    KNOWLEDGE_DATA = "No policy data available."

# =====================================================================
# [NEW] LOAD RAG TEMPLATE (TỪ ĐIỂN PHÉP THUẬT MIX ẢNH)
# =====================================================================
TEMPLATES_FILE = "app/templates.json"
TEMPLATES_DATA = {}
try:
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        TEMPLATES_DATA = json.load(f)
except Exception as e:
    print(f"Lỗi đọc file templates: {e}")


# =====================================================================
# 1. BỘ NÃO ĐIỀU PHỐI (CONVERSATION API - TỐI ƯU PROMPT CACHING)
# =====================================================================

# Cache module-level: chỉ rebuild base prompt khi sang ngày mới
_CACHED_DATE_STR = None
_BASE_SYSTEM_PROMPT = None


def _get_base_system_prompt() -> str:
    """
    Trả về base system prompt (SYSTEM_PROMPT + KNOWLEDGE_DATA).
    Chỉ rebuild 1 lần/ngày — đảm bảo byte-for-byte giống nhau giữa
    mọi request trong cùng ngày → cache hit tối đa cho phần nặng nhất.
    """
    global _CACHED_DATE_STR, _BASE_SYSTEM_PROMPT
    today_str = datetime.now().strftime("%A, %B %d, %Y")
    if _CACHED_DATE_STR != today_str or _BASE_SYSTEM_PROMPT is None:
        _CACHED_DATE_STR = today_str
        _BASE_SYSTEM_PROMPT = f"""{prompts.SYSTEM_PROMPT}

### [SYSTEM KNOWLEDGE BASE - MANDATORY READ]
- Reference Date: {today_str}
{KNOWLEDGE_DATA}"""
        print(f"🔄 [Cache] Base system prompt rebuilt for: {today_str}")
    return _BASE_SYSTEM_PROMPT


def call_conversation_api(
    user_message: str,
    session_id: str,
    image_base64_list: list = None,
    last_generated_image_url: str = None,
    template_id: str = None,
):
    import hashlib

    last_entities = database.get_last_entities(session_id)
    history = database.get_history(session_id) 

    messages = [{"role": "system", "content": _get_base_system_prompt()}]

    if template_id and template_id in TEMPLATES_DATA:
        rule = TEMPLATES_DATA[template_id].get("master_prompt_rule", "")
        messages.append(
            {
                "role": "system",
                "content": f"### [ACTIVE TEMPLATE RULE: {template_id}]\n{rule}",
            }
        )

    messages.extend(history)

    user_content = []

    if user_message:
        user_content.append({"type": "text", "text": user_message})

    if image_base64_list and isinstance(image_base64_list, list):
        for idx, img_b64 in enumerate(image_base64_list):
            valid_b64_url = get_valid_image_url(img_b64)

            if template_id == "multi_subject_merge":
                label = f"[ẢNH {idx + 1} - CHỦ THỂ SỐ {idx + 1} (SUBJECT {idx + 1})]:"
            elif template_id == "style_transfer":
                label = "[ẢNH 1 - CHỦ THỂ (MAIN SUBJECT)]:"
            elif template_id and len(image_base64_list) == 2:
                label = (
                    "[ẢNH 1 - CHỦ THỂ (MAIN SUBJECT)]:"
                    if idx == 0
                    else "[ẢNH 2 - MẪU STYLE (STYLE REFERENCE)]:"
                )
            else:
                label = f"[Ảnh {idx + 1} do khách gửi]:"

            user_content.append({"type": "text", "text": label})
            user_content.append(
                {"type": "image_url", "image_url": {"url": valid_b64_url}}
            )

    if last_generated_image_url:
        valid_b64_url = get_valid_image_url(last_generated_image_url)
        user_content.append(
            {
                "type": "text",
                "text": "[SYSTEM Context]: This is the design you generated in the previous turn. Use this as the primary visual context for requested edits.",
            }
        )
        user_content.append({"type": "image_url", "image_url": {"url": valid_b64_url}})

    messages.append({"role": "user", "content": user_content})

    if last_entities and any(v is not None for v in last_entities.values()):
        entities_str = json.dumps(last_entities, ensure_ascii=False)
        messages.append(
            {
                "role": "system",
                "content": (
                    f"[SYSTEM MEMORY]: Previously confirmed data: {entities_str}. "
                    f"You MUST maintain these non-null values in your new JSON response."
                ),
            }
        )

    headers = {
        "Authorization": f"Bearer {config.XAI_API_KEY}",
        "Content-Type": "application/json",
        "x-grok-conv-id": session_id,
    }

    payload = {
        "model": config.CONVERSATION_MODEL,
        "messages": messages,
        "temperature": config.CHAT_TEMPERATURE,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "pod_output",
                "strict": True,
                "schema": prompts.JSON_SCHEMA,
            },
        },
        "store": config.STORE_CONTEXT,
        "reasoning": {"effort": config.REASONING_EFFORT},
    }

    l1 = messages[0]["content"]
    total_msgs = len(messages)
    history_pairs = len(history) // 2
    print(
        f"📤 [Chat] session={session_id[:12]} | "
        f"msgs={total_msgs} (history={history_pairs} turns) | "
        f"L1_hash={hashlib.md5(l1.encode()).hexdigest()[:8]}"
    )

    try:
        response = requests.post(
            f"{config.XAI_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )

        if response.status_code != 200:
            print(f"Chat API Error ({response.status_code}): {response.text[:500]}")
            return None

        res_data = response.json()

        usage = res_data.get("usage", {})
        total_tokens = usage.get("prompt_tokens", 0)
        cached_tokens = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)

        if total_tokens > 0:
            hit_rate = (cached_tokens / total_tokens) * 100
            billed = total_tokens - cached_tokens
            status = "🔥" if hit_rate >= 70 else ("⚡" if hit_rate >= 30 else "❄️")
            print(
                f"{status} [Cache] Total: {total_tokens:,} | "
                f"Cached: {cached_tokens:,} ({hit_rate:.1f}%) | "
                f"Billed: {billed:,} | Template: {template_id or 'none'}"
            )

        raw_text = res_data["choices"][0]["message"]["content"]
        ai_json = json.loads(raw_text)

        database.update_session_data(
            session_id=session_id,
            response_id=res_data.get("id"),
            entities=ai_json.get("entities_extracted"),
            new_user_msg=user_content,
            new_assistant_text=raw_text,
        )

        return ai_json

    except Exception as e:
        print(f"Call Conversation API Exception: {e}")
        return None


def _to_base64_url(path: str, preprocess: bool = False) -> str | None:
    if not path:
        return None
    if path.startswith("data:"):
        return path
    if path.startswith("/uploads/") or path.startswith("/design_outputs/"):
        file_path = os.path.join(_PROJECT_ROOT, path.lstrip("/"))
        if not os.path.exists(file_path):
            return None

        if preprocess:
            try:
                img = Image.open(file_path)
                img_processed = _smart_crop_to_content(img)
                buf = io.BytesIO()
                img_processed.save(buf, format="JPEG", quality=95)
                encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
                return f"data:image/jpeg;base64,{encoded}"
            except Exception as e:
                print(f"  ⚠️  Preprocess failed ({e}), using original")

        ext = file_path.rsplit(".", 1)[-1].lower()
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        with open(file_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        return f"data:{mime};base64,{encoded}"
    return None


def call_image_generation(
    master_prompt: str,
    reference_images: list = None,
    is_retry: bool = False,
):
    output_dir = os.path.join(_PROJECT_ROOT, "design_outputs")
    os.makedirs(output_dir, exist_ok=True)

    pod_output_instruction = """
    CRITICAL PRINT-ON-DEMAND REQUIREMENT:
    - SCALE UP THE SUBJECT MASSIVELY. The character, wings, aura, or design elements MUST stretch horizontally to touch or bleed off the left and right edges. 
    - DO NOT leave empty black margins on the sides. NO inner border, NO contained-frame effect.
    - Pure solid BLACK background absolute hex #000000. NO gradients, NO shadows, NO environment.
    """

    if is_retry:
        full_prompt = master_prompt
    else:
        full_prompt = master_prompt + "\n\n" + pod_output_instruction

    selected_model = config.IMAGE_MODEL_EDIT if is_retry else config.IMAGE_MODEL_GEN

    try:
        if reference_images and len(reference_images) > 0:
            print(
                f"🖼️ Using Grok Imagine Edits (is_retry={is_retry}) | Model: {selected_model}"
            )

            image_payloads = []
            for img in reference_images[:3]:
                img_input = _to_base64_url(img) or img
                image_payloads.append({"type": "image_url", "url": img_input})

            response = requests.post(
                f"{config.XAI_BASE_URL}/images/edits",
                headers={
                    "Authorization": f"Bearer {config.XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": selected_model,
                    "prompt": full_prompt,
                    "n": 1,
                    "images": image_payloads,
                },
                timeout=180,
            )

        else:
            print(
                f"✨ Using Grok Imagine Generations (is_retry={is_retry}) | Model: {selected_model}"
            )
            response = requests.post(
                f"{config.XAI_BASE_URL}/images/generations",
                headers={
                    "Authorization": f"Bearer {config.XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": selected_model,
                    "prompt": full_prompt,
                    "n": 1,
                },
                timeout=180,
            )

        if response.status_code != 200:
            error_msg = response.text[:1000]
            print(f"❌ Image API Error ({response.status_code}): {error_msg}")
            return {"image_url": None, "error": error_msg}

        img_data = response.json()["data"][0]

        # Convert qua chuẩn RGB để lưu JPG
        if "url" in img_data:
            img_bytes = requests.get(img_data["url"], timeout=60).content
            img_raw = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        elif "b64_json" in img_data:
            img_raw = Image.open(
                io.BytesIO(base64.b64decode(img_data["b64_json"]))
            ).convert("RGB")
        else:
            raise Exception("No image returned from API")

        print("💾 Saving raw image from Grok...")
        # Ép vuông 1:1 — crop center nếu không vuông, tránh letterbox
        w, h = img_raw.size
        if w != h:
            side = min(w, h)
            left = (w - side) // 2
            top  = (h - side) // 2
            img_raw = img_raw.crop((left, top, left + side, top + side))
            print(f"  ✂️  Cropped {w}x{h} → {side}x{side} (center square)")
        if img_raw.size != (1024, 1024):
            img_raw = img_raw.resize((1024, 1024), Image.Resampling.LANCZOS)
            print("  🔲 Resized to 1024x1024")
        filename = f"preview_raw_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
        img_raw.save(os.path.join(output_dir, filename), format="JPEG", quality=95)
        return {"image_url": f"/design_outputs/{filename}", "error": None}
    except Exception as e:
        print(f"❌ call_image_generation Exception: {e}")
        return {"image_url": None, "error": str(e)}


def finalize_image(preview_image_url: str):
    """
    Chốt ảnh final — KHÔNG gọi AI, KHÔNG edit gì thêm.
    Bản JPG: Không xử lý Alpha, đưa toàn bộ vào nền trắng chuẩn RGB.
    """
    output_dir = os.path.join(_PROJECT_ROOT, "design_outputs")
    os.makedirs(output_dir, exist_ok=True)

    try:
        if preview_image_url.startswith("/design_outputs/"):
            file_path = os.path.join(_PROJECT_ROOT, preview_image_url.lstrip("/"))
        else:
            return {
                "image_url": None,
                "error": "Invalid preview_image_url — must be an /design_outputs/ path",
            }

        if not os.path.exists(file_path):
            return {"image_url": None, "error": f"Preview file not found: {file_path}"}

        print(f"🚀 Finalizing from preview: {preview_image_url}")

        # Đọc ảnh preview - đổi thành chuẩn RGB cho JPG
        img = Image.open(file_path).convert("RGB")

        # ── BƯỚC 1: Upscale RGB lên canvas target bằng LANCZOS ──────────────────
        target_w, target_h = 4500, 5400
        # Gỡ bỏ hoàn toàn padding, cho phép ảnh phóng to 100% chạm viền canvas
        max_w = target_w  
        max_h = target_h
        aspect = img.width / img.height
        if aspect > (max_w / max_h):
            new_w = max_w
            new_h = int(max_w / aspect)
        else:
            new_w = int(max_h * aspect)
            new_h = max_h

        rgb_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # ── BƯỚC 2: Sharpen RGB sau upscale để bù độ mờ do kéo giãn ────────────
        rgb_sharp = rgb_resized.filter(
            ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)
        )

        # ── BƯỚC 3: Tạo canvas ĐEN tĩnh chuẩn RGB và paste ảnh lên ──────────
        canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0)) # <--- Đổi thành 0,0,0
        paste_x = (target_w - new_w) // 2
        paste_y = (target_h - new_h) // 2
        canvas.paste(rgb_sharp, (paste_x, paste_y))

        # ── BƯỚC 4: Lưu file JPG 300 DPI chuẩn xưởng in ────────────────────────
        filename = f"pod_design_final_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
        canvas.save(
            os.path.join(output_dir, filename),
            format="JPEG",
            dpi=(300, 300),
            quality=95,
            optimize=True,
        )
        print(f"✅ Final saved: {filename} ({target_w}x{target_h} @ 300 DPI)")
        return {"image_url": f"/design_outputs/{filename}", "error": None}

    except Exception as e:
        print(f"❌ finalize_image Exception: {e}")
        return {"image_url": None, "error": str(e)}


# =====================================================================
# 3. ROUTES & ENPOINTS (FASTAPI)
# =====================================================================
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/templates")
async def get_templates():
    return JSONResponse(content=TEMPLATES_DATA)


# =====================================================================
# LOAD MOCKUPS (DANH SÁCH SẢN PHẨM XEM THỬ)
# =====================================================================
MOCKUPS_FILE = "app/mockups.json"
MOCKUPS_DATA = []
try:
    with open(MOCKUPS_FILE, "r", encoding="utf-8") as f:
        MOCKUPS_DATA = json.load(f)
except Exception as e:
    print(f"Lỗi đọc file mockups: {e}")


@app.get("/api/mockups")
async def get_mockups():
    return JSONResponse(content=MOCKUPS_DATA)


@app.post("/chat")
async def chat_endpoint(request: Request):
    data = await request.json()
    user_input = data.get("message")
    image_list = data.get("image_base64_list")
    last_gen_url = data.get("last_generated_image_url")
    session_id = data.get("session_id")
    template_id = data.get("template_id")

    if not session_id:
        session_id = f"session_{int(time.time())}_{hash(str(request.client.host))}"

    ai_response = call_conversation_api(
        user_input, session_id, image_list, last_gen_url, template_id
    )

    if not ai_response:
        return JSONResponse(
            content={
                "user_reply": "Oops, my network is lagging a bit! Could you resend that? 😅"
            },
            status_code=500,
        )

    return JSONResponse(content=ai_response)


@app.post("/generate-image")
async def generate_image_endpoint(request: Request):
    data = await request.json()
    master_prompt = data.get("master_prompt")
    reference_images = data.get("reference_images")
    is_retry = data.get("is_retry", False)

    if not master_prompt:
        return JSONResponse(content={"error": "Missing master_prompt"}, status_code=400)

    img_res = call_image_generation(
        master_prompt=master_prompt,
        reference_images=reference_images,
        is_retry=is_retry,
    )
    return JSONResponse(content=img_res)


@app.post("/retry-image")
async def retry_image_endpoint(request: Request):
    data = await request.json()
    retry_instruction = data.get("retry_instruction")
    preview_image_url = data.get("preview_image_url")
    original_master_prompt = data.get("original_master_prompt", "")

    if not retry_instruction:
        return JSONResponse(
            content={"error": "Missing retry_instruction"}, status_code=400
        )
    if not preview_image_url:
        return JSONResponse(
            content={"error": "Missing preview_image_url"}, status_code=400
        )

    # Tạo edit prompt trực tiếp — KHÔNG qua conversation API để tránh nhiễu luật
    # Chỉ nói rõ: giữ nguyên ảnh gốc, chỉ sửa đúng phần khách yêu cầu
    if original_master_prompt:
        edit_prompt = (
            f"You are editing an existing design image. "
            f"CRITICAL: Keep the overall composition, subject identity, art style, "
            f"colors, and all other elements EXACTLY as they appear in the provided image. "
            f"Apply ONLY this specific change: {retry_instruction}\n\n"
            f"Original design context (for reference only): {original_master_prompt[:400]}"
        )
    else:
        edit_prompt = (
            f"You are editing an existing design image. "
            f"CRITICAL: Keep the overall composition, subject identity, art style, "
            f"colors, and all other elements EXACTLY as they appear in the provided image. "
            f"Apply ONLY this specific change: {retry_instruction}"
        )

    print(f"\u270f\ufe0f Retry \u2014 edit prompt:\n{edit_prompt[:300]}...")

    img_res = call_image_generation(
        master_prompt=edit_prompt,
        reference_images=[preview_image_url],
        is_retry=True,
    )
    return JSONResponse(content=img_res)



@app.post("/finalize-image")
async def finalize_image_endpoint(request: Request):
    data = await request.json()
    preview_image_url = data.get("preview_image_url")

    if not preview_image_url:
        return JSONResponse(
            content={"error": "Missing preview_image_url"}, status_code=400
        )

    img_res = finalize_image(preview_image_url)
    return JSONResponse(content=img_res)


@app.post("/reset")
async def reset_endpoint(request: Request):
    data = await request.json()
    session_id = data.get("session_id")
    if session_id:
        database.delete_session(session_id)
    return JSONResponse(
        content={"status": "success", "message": "Memory cleared successfully!"}
    )


@app.post("/api/design-bg-color")
async def design_bg_color_endpoint(request: Request):
    """
    Nhận image_url của design, detect màu nền bằng cách sample viền ngoài.
    Trả về { "hex": "#RRGGBB", "r": 0, "g": 0, "b": 0 }
    """
    data = await request.json()
    image_url = data.get("image_url", "")

    # Lấy ảnh — có thể là /design_outputs/... (local) hoặc URL ngoài
    img_array = None

    if image_url.startswith("/design_outputs/"):
        file_path = os.path.join(_PROJECT_ROOT, image_url.lstrip("/"))
        if os.path.exists(file_path):
            try:
                pil_img = Image.open(file_path).convert("RGB")
                img_array = np.array(pil_img)
            except Exception as e:
                return JSONResponse(content={"error": str(e)}, status_code=500)
    elif image_url.startswith("http"):
        try:
            resp = requests.get(image_url, timeout=10)
            pil_img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            img_array = np.array(pil_img)
        except Exception as e:
            return JSONResponse(content={"error": str(e)}, status_code=500)
    elif image_url.startswith("data:image"):
        try:
            header, b64data = image_url.split(",", 1)
            raw = base64.b64decode(b64data)
            pil_img = Image.open(io.BytesIO(raw)).convert("RGB")
            img_array = np.array(pil_img)
        except Exception as e:
            return JSONResponse(content={"error": str(e)}, status_code=500)

    if img_array is None:
        return JSONResponse(content={"error": "Cannot load image"}, status_code=400)

    h, w = img_array.shape[:2]
    bh = max(4, int(h * 0.06))
    bw = max(4, int(w * 0.06))

    # Sample 4 dải viền ngoài
    border_pixels = np.concatenate([
        img_array[:bh,   :,    :].reshape(-1, 3),   # top
        img_array[h-bh:, :,    :].reshape(-1, 3),   # bottom
        img_array[:,  :bw,     :].reshape(-1, 3),   # left
        img_array[:,  w-bw:,   :].reshape(-1, 3),   # right
    ])

    # [CẬP NHẬT MỚI]: Chỉ lọc lấy các pixel thuộc nhóm màu tối (Đen/Gần đen)
    dark_pixels = border_pixels[(border_pixels[:, 0] < 40) & 
                                (border_pixels[:, 1] < 40) & 
                                (border_pixels[:, 2] < 40)]

    # Nếu tìm thấy pixel tối, lấy nhóm đó. Nếu viền không có màu tối thì giữ nguyên viền gốc
    target_pixels = dark_pixels if len(dark_pixels) > 0 else border_pixels

    # Median bền vững hơn mean khi viền có 1 chút content
    median_rgb = np.median(target_pixels, axis=0).astype(int)
    r, g, b = int(median_rgb[0]), int(median_rgb[1]), int(median_rgb[2])
    hex_color = f"#{r:02X}{g:02X}{b:02X}"

    print(f"🎨 Design BG color detected (Dark Target): {hex_color} (R={r}, G={g}, B={b})")
    return JSONResponse(content={"hex": hex_color, "r": r, "g": g, "b": b})

@app.post("/api/save-mockup")
async def save_mockup_endpoint(request: Request):
    """
    Nhận ảnh Mockup từ Frontend (định dạng base64) và tự động lưu vào thư mục mockups_outputs
    """
    data = await request.json()
    b64_data = data.get("image_base64")
    if not b64_data:
        return JSONResponse(content={"error": "Missing image data"}, status_code=400)

    try:
        # Tách lấy phần data thật (bỏ cái mác header 'data:image/png;base64,')
        if "," in b64_data:
            header, encoded = b64_data.split(",", 1)
        else:
            encoded = b64_data

        image_bytes = base64.b64decode(encoded)
        # Tạo tên file có chứa chữ mockup để phân biệt
        filename = f"mockup_saved_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        filepath = os.path.join(_PROJECT_ROOT, "mockups_outputs", filename)

        # Ghi file vào thư mục mới
        with open(filepath, "wb") as f:
            f.write(image_bytes)

        print(f"👕 Đã lưu Mockup tự động thành công: {filename}")
        return JSONResponse(content={"url": f"/mockups_outputs/{filename}", "success": True})
    except Exception as e:
        print(f"❌ Lỗi khi lưu mockup: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)