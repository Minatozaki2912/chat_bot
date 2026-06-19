import json
import os
import re
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
import app.template_prompt as template_prompt
import asyncio

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
# KHỞI ĐỘNG: ĐỌC TẤT CẢ FILE LUẬT — CHỈ 1 LẦN DUY NHẤT
# =====================================================================
print("━" * 60)
print("🚀 [STARTUP] Đang tải các file luật vào bộ nhớ...")

KNOWLEDGE_FILE = "app/knowledge.json"
KNOWLEDGE_DATA = ""
try:
    with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
        KNOWLEDGE_DATA = f.read()
    print(f"  ✅ knowledge.json       — {len(KNOWLEDGE_DATA):,} chars")
except Exception as e:
    KNOWLEDGE_DATA = "No policy data available."
    print(f"  ❌ knowledge.json       — Lỗi: {e}")

TEMPLATES_FILE = "app/templates.json"
TEMPLATES_DATA = {}
try:
    with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
        TEMPLATES_DATA = json.load(f)
    _names = [v.get("name", k) for k, v in TEMPLATES_DATA.items()]
    print(f"  ✅ templates.json       — {len(TEMPLATES_DATA)} templates: {', '.join(_names)}")
except Exception as e:
    print(f"  ❌ templates.json       — Lỗi: {e}")

print(f"  ✅ prompts.py           — {len(prompts.SYSTEM_PROMPT):,} chars")


# =====================================================================
# 1. BỘ NÃO ĐIỀU PHỐI (CONVERSATION API - TỐI ƯU PROMPT CACHING)
# =====================================================================

# THÊM 2 DÒNG NÀY VÀO NGAY TRÊN ĐẦU HÀM
_CACHED_DATE_STR = None
_BASE_SYSTEM_PROMPT = None

def _get_base_system_prompt(template_id: str = None, force_reload: bool = False) -> str:
    """
    Build Base System Prompt. 
    Tự động chia tách bộ não xử lý dựa trên chế độ chat thường hoặc template.
    """
    global _CACHED_DATE_STR, _BASE_SYSTEM_PROMPT, KNOWLEDGE_DATA, TEMPLATES_DATA
    today_str = datetime.now().strftime("%A, %B %d, %Y")
    
    if force_reload or _CACHED_DATE_STR != today_str or _BASE_SYSTEM_PROMPT is None:
        _CACHED_DATE_STR = today_str

        # Đọc lại file từ ổ cứng khi có cập nhật mới
        try:
            with open("app/knowledge.json", "r", encoding="utf-8") as f:
                KNOWLEDGE_DATA = f.read()
            with open("app/templates.json", "r", encoding="utf-8") as f:
                TEMPLATES_DATA = json.load(f)
        except Exception as e:
            print(f"⚠️ Lỗi khi load lại file luật: {e}")

        # Đóng gói kho dữ liệu Template thành chuỗi văn bản
        templates_str = ""
        if isinstance(TEMPLATES_DATA, dict):
            for t_id, t_data in TEMPLATES_DATA.items():
                rule = t_data.get('master_prompt_rule', '')
                templates_str += f"\n--- [TEMPLATE ID: {t_id}] ---\n{rule}\n"

        # === KHỐI RẼ NHÁNH PROMPT CHO QUÁ TRÌNH KIỂM THỬ ===
        if template_id and template_id in TEMPLATES_DATA:
            # CHẾ ĐỘ TEMPLATE KHẮT KHE: Chỉ dùng lõi luật tinh gọn của template_prompt
            _BASE_SYSTEM_PROMPT = f"""{template_prompt.SYSTEM_PROMPT}

### [SYSTEM KNOWLEDGE BASE]
- Reference Date: {today_str}

### [MAGIC TEMPLATES DICTIONARY]
The current ACTIVE TEMPLATE ID is: {template_id}.
You MUST read its rule from the dictionary below and apply it immediately:
{templates_str}
"""
            print(f"🔄 [Cache] Chế độ TEMPLATE được kích hoạt cho ID: {template_id}")
        else:
            # CHẾ ĐỘ CHAT TỰ DO / BRAINSTORM: Dùng file prompts.py gốc chứa đầy đủ 8 chiến lược
            _BASE_SYSTEM_PROMPT = f"""{prompts.SYSTEM_PROMPT}

### [SYSTEM KNOWLEDGE BASE - MANDATORY READ]
- Reference Date: {today_str}
{KNOWLEDGE_DATA}

### [MAGIC TEMPLATES DICTIONARY]
{templates_str}
"""
            print(f"🔄 Chế độ CHAT TỰ DO được kích hoạt.")
            
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

    # ĐIỂM CẬP NHẬT 1: Truyền biến template_id vào hàm lấy System Prompt để rẽ nhánh bộ não
    system_message = {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": _get_base_system_prompt(template_id=template_id),
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }

    messages = [system_message]
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
            elif template_id == "face_character_swap":
                label = "[ẢNH 1 - CHỦ THỂ (MAIN SUBJECT TO COSPLAY)]:"
            elif template_id == "true_subject_replacement":
                label = "[ẢNH 1 - ẢNH GỐC, GIỮ NGUYÊN BỐI CẢNH (ORIGINAL IMAGE, KEEP BACKGROUND)]:"
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

    # ĐIỂM CẬP NHẬT 2: Chọn cấu trúc Schema động tương ứng dựa theo luồng chat
    active_schema = template_prompt.JSON_SCHEMA if template_id else prompts.JSON_SCHEMA

    payload = {
        "model": config.CONVERSATION_MODEL,
        "messages": messages,
        "temperature": config.CHAT_TEMPERATURE,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "pod_output",
                "strict": True,
                "schema": active_schema,
            },
        },
        "store": config.STORE_CONTEXT,
        "reasoning": {"effort": config.REASONING_EFFORT},
    }

    total_msgs = len(messages)
    history_pairs = len(history) // 2
    # content là list[dict] nên lấy text từ phần tử đầu để hash
    l1_content = messages[0]["content"]
    l1_text = l1_content[0]["text"] if isinstance(l1_content, list) else l1_content
    is_new_session = history_pairs == 0
    print(
        f"📤 [Chat] session={session_id} | "
        f"{'🆕 new session' if is_new_session else f'turn {history_pairs + 1}'} | "
        f"template={template_id or 'none'} | "
        f"cache={'MISS (first)' if is_new_session else 'HIT'} | "
        f"L1_hash={hashlib.md5(l1_text.encode()).hexdigest()[:8]}"
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


def _compress_prompt(prompt: str, limit: int = 7500) -> str:
    """
    Nén master_prompt thông minh: KHÔNG BAO GIỜ cắt phần miêu tả Subject.
    Chỉ xóa bỏ các câu lệnh meta, dặn dò thừa thãi để tiết kiệm token.
    """
    if len(prompt) <= limit:
        return prompt

    original_len = len(prompt)

    # 1. Rút gọn rule Canvas & Typography dài dòng của AI
    prompt = re.sub(
        r"CANVAS: uniform pure #000000 edge-to-edge.*?meta text visible\.", 
        "Black #000000 canvas background edge-to-edge. ", 
        prompt, flags=re.IGNORECASE | re.DOTALL
    )
    prompt = re.sub(
        r"Typography:.*?never cropped.*?overflow the canvas\.",
        r"Typography fully visible, safe padding ALL sides, never cropped. ",
        prompt, flags=re.IGNORECASE | re.DOTALL
    )

    # Nếu vẫn quá dài, chỉ cắt phần đuôi cứng (tránh cắt đứt câu giữa chừng)
    if len(prompt) > limit:
        prompt = prompt[:limit]
        last_period = max(prompt.rfind(". "), prompt.rfind(".\n"))
        if last_period > limit - 1000:
            prompt = prompt[:last_period + 1]

    print(f"🗜️  [Prompt Compress] Smart truncated: {original_len} → {len(prompt)} chars (Subject Preserved)")
    return prompt


def call_image_generation(
    master_prompt: str,
    reference_images: list = None,
    is_retry: bool = False,
    template_id: str = None,
):
    output_dir = os.path.join(_PROJECT_ROOT, "design_outputs")
    os.makedirs(output_dir, exist_ok=True)

    # Rút gọn so với phiên bản cũ (~300 chars) — giữ đúng ý, bỏ verbose
    pod_output_instruction = "CRITICAL POD: Subject fills canvas edge-to-edge, no empty side margins, no inner border. Pure solid black #000000 background, no gradients, no environment."

    # Đọc marker [SCENE_MODE: ...] do AI chat tự đặt ở đầu master_prompt
    cleaned_prompt = master_prompt
    is_full_scene_mode = False

    scene_mode_match = re.match(
        r"^\s*\[SCENE_MODE:\s*(BLACK_CANVAS|FULL_SCENE)\]\s*\n?",
        master_prompt,
        re.IGNORECASE,
    )
    if scene_mode_match:
        is_full_scene_mode = scene_mode_match.group(1).upper() == "FULL_SCENE"
        # Xóa marker khỏi prompt thật gửi cho AI vẽ ảnh — đây chỉ là tín hiệu nội bộ
        cleaned_prompt = master_prompt[scene_mode_match.end():].lstrip()
    else:
        # Fallback an toàn nếu AI chat quên thêm marker (model cũ hoặc lỗi)
        LEGACY_FULL_SCENE_TEMPLATES = {"subject_replacement_layout"}
        is_full_scene_mode = template_id in LEGACY_FULL_SCENE_TEMPLATES

    skip_black_background = is_retry or is_full_scene_mode

    if skip_black_background:
        full_prompt = cleaned_prompt
    else:
        full_prompt = cleaned_prompt + "\n\n" + pod_output_instruction

    # ── Nén prompt xuống dưới giới hạn Image API (8000 chars) ──────────────
    original_len = len(full_prompt)
    full_prompt = _compress_prompt(full_prompt, limit=7500)
    if len(full_prompt) != original_len:
        print(f"🗜️  [Prompt] {original_len} → {len(full_prompt)} chars after compression")

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
        # GIỮ NGUYÊN TỶ LỆ, PAD NỀN ĐEN NẾU KHÔNG VUÔNG ĐỂ KHÔNG BỊ CẮT CHỦ THỂ
        w, h = img_raw.size
        if w != 1024 or h != 1024:
            # Thu nhỏ ảnh sao cho cạnh dài nhất = 1024, giữ nguyên tỷ lệ
            img_raw.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            new_w, new_h = img_raw.size
            
            # Tạo canvas vuông nền đen #000000 chuẩn 1024x1024
            background = Image.new("RGB", (1024, 1024), (0, 0, 0))
            
            # Dán ảnh đã thu nhỏ vào chính giữa canvas
            offset_x = (1024 - new_w) // 2
            offset_y = (1024 - new_h) // 2
            background.paste(img_raw, (offset_x, offset_y))
            
            img_raw = background
            print(f"  🔲 Resized & Padded to 1024x1024 (from {w}x{h})")
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

@app.post("/api/warmup-cache")
async def warmup_cache_endpoint(request: Request):
    try:
        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
        force = body.get("force_reload", False)

        system_message = {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": _get_base_system_prompt(force_reload=force),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }

        payload = {
            "model": config.CONVERSATION_MODEL,
            "messages": [system_message],
            "max_tokens": 1,
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

        headers = {
            "Authorization": f"Bearer {config.XAI_API_KEY}",
            "Content-Type": "application/json",
        }

        # Chạy nền — không chờ kết quả, không block
        def _fire():
            try:
                requests.post(
                    f"{config.XAI_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60,  # timeout dài hơn vì chạy nền
                )
                print("🔥 [Cache Warming] Đã mồi & gia hạn Prompt Caching thành công!")
            except Exception as e:
                print(f"⚠️ [Cache Warming] Lỗi nền: {e}")

        asyncio.get_event_loop().run_in_executor(None, _fire)
        return JSONResponse(content={"status": "warming_in_background"})

    except Exception as e:
        print(f"⚠️ [Cache Warming] Bỏ qua lỗi mồi cache: {e}")
        return JSONResponse(content={"status": "skipped"})
    
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
        print(f"\U0001f195 [Session] New session created: {session_id[:24]}...")
        print(f"🆕 [Session] New session created: {session_id[:24]}...")

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
    template_id = data.get("template_id")

    if not master_prompt:
        return JSONResponse(content={"error": "Missing master_prompt"}, status_code=400)

    img_res = call_image_generation(
        master_prompt=master_prompt,
        reference_images=reference_images,
        is_retry=is_retry,
        template_id=template_id,
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

@app.post("/analyze-style")
async def analyze_style_endpoint(request: Request):
    """
    Nhận ảnh style (base64), gọi AI chat để phân tích kỹ thuật nghệ thuật,
    trả về style description text dùng cho image generation.
    Tuyệt đối KHÔNG gửi ảnh này vào image generation API.
    """
    data = await request.json()
    style_image_b64 = data.get("style_image")

    if not style_image_b64:
        return JSONResponse(content={"error": "Missing style_image"}, status_code=400)

    # Đảm bảo format đúng
    if not style_image_b64.startswith("data:"):
        style_image_b64 = f"data:image/jpeg;base64,{style_image_b64}"

    analyze_prompt = (
        "[STYLE ANALYSIS TASK]: Analyze the artistic technique of this image. "
        "Describe ONLY the visual craft medium and rendering style — NOT the subject, characters, or content. "
        "Output a compact style descriptor (max 30 words) suitable for an image generation prompt. "
        "Focus on: drawing medium, line art style, coloring technique, texture, shading method. "
        "Examples: 'bold cel-shaded anime ink outlines, vibrant flat colors' or "
        "'soft watercolor washes, wet-on-wet bleeding edges, charcoal sketch lines'. "
        "Return ONLY the style descriptor string, nothing else."
    )

    # Gọi AI chat với chỉ ảnh style — session riêng biệt, không lưu history
    temp_session = f"style_analyze_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    headers = {
        "Authorization": f"Bearer {config.XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": config.CONVERSATION_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a visual art style analyst. Analyze artistic techniques only.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": analyze_prompt},
                    {"type": "image_url", "image_url": {"url": style_image_b64}},
                ],
            },
        ],
        "max_tokens": 120,
        "temperature": 0.3,
    }

    try:
        resp = requests.post(
            f"{config.XAI_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        style_text = result["choices"][0]["message"]["content"].strip()
        # Bỏ dấu ngoặc kép nếu AI tự thêm vào
        style_text = style_text.strip('"').strip("'")
        print(f"🎨 [Style Analysis] Result: {style_text[:80]}...")
        return JSONResponse(content={"style_text": style_text})
    except Exception as e:
        print(f"❌ [Style Analysis] Error: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

        
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)