import json
import os
import uuid
import requests
import time
import io
import base64
from PIL import Image, ImageDraw
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

os.makedirs("outputs", exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")
# ==================== PROJECT ROOT ====================
import pathlib

_PROJECT_ROOT = str(pathlib.Path(__file__).parent.parent.absolute())
print(f"✅ PROJECT_ROOT initialized: {_PROJECT_ROOT}")


# =====================================================================
# HÀM MÃ HÓA ẢNH NỘI BỘ THÀNH BASE64 (ĐỂ AI ĐỌC ĐƯỢC)
# =====================================================================
def get_valid_image_url(image_path_or_url):
    """
    Biến đổi link nội bộ (/outputs/...) thành chuỗi Base64.
    Nếu đã là base64 sẵn từ Frontend, hoặc là link web ngoài thì giữ nguyên.
    """
    if image_path_or_url and image_path_or_url.startswith("/outputs/"):
        local_file = image_path_or_url.lstrip("/")
        try:
            with open(local_file, "rb") as f:
                b64_str = base64.b64encode(f.read()).decode("utf-8")
                return f"data:image/png;base64,{b64_str}"
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
    """
    Gửi messages theo đúng chuẩn Grok prompt caching:
    - messages[0]: system prompt tĩnh (cache cả ngày)
    - messages[1]: template rule nếu có (bán tĩnh)
    - messages[2..N-1]: lịch sử hội thoại đầy đủ (user+assistant từ DB)
      → đây là prefix mà Grok cần để cache hit
    - messages[N]: user message mới nhất (append cuối cùng)
    - messages[N+1] (system): context động (thời gian + memory)
      → đặt cuối để không phá cache của prefix phía trước
    """
    import hashlib

    last_entities = database.get_last_entities(session_id)
    history = database.get_history(session_id)  # list [user, asst, user, asst, ...]

    # =====================================================================
    # LAYER 1 — TUYỆT ĐỐI TĨNH → rebuild 1 lần/ngày
    # =====================================================================
    messages = [{"role": "system", "content": _get_base_system_prompt()}]

    # =====================================================================
    # LAYER 2 — BÁN TĨNH → template rule (tách riêng để L1 không bị phá)
    # =====================================================================
    if template_id and template_id in TEMPLATES_DATA:
        rule = TEMPLATES_DATA[template_id].get("master_prompt_rule", "")
        messages.append(
            {
                "role": "system",
                # THÊM TEMPLATE_ID VÀO ĐÂY ĐỂ AI NHẬN DIỆN:
                "content": f"### [ACTIVE TEMPLATE RULE: {template_id}]\n{rule}",
            }
        )

    # =====================================================================
    # LAYER 3 — LỊCH SỬ HỘI THOẠI → append toàn bộ user+assistant cũ
    # Đây là phần quan trọng nhất cho cache:
    # Grok so sánh prefix từng byte — nếu messages[0..N-1] khớp hoàn toàn
    # với lượt trước thì cache hit. History được lưu sẵn trong DB, không
    # tái tạo → đảm bảo byte-for-byte giống nhau.
    # =====================================================================
    messages.extend(history)

    # ==================== USER MESSAGE MỚI ====================
    # Thời gian HH:MM đã bị loại bỏ hoàn toàn — không có rule nào trong
    # system prompt cần đến giờ phút, nên nó chỉ phá cache mà không có giá trị.
    # [SYSTEM MEMORY] vẫn giữ nhưng đặt RIÊNG thành system message SAU user
    # message và KHÔNG lưu vào DB history → không phá prefix cache.
    user_content = []

    if user_message:
        user_content.append({"type": "text", "text": user_message})

    if image_base64_list and isinstance(image_base64_list, list):
        for idx, img_b64 in enumerate(image_base64_list):
            valid_b64_url = get_valid_image_url(img_b64)

            # Logic phân loại nhãn động theo từng template
            if template_id == "multi_subject_merge":
                # Nhãn dành riêng cho template ghép ảnh (hỗ trợ tới 3 chủ thể đồng cấp)
                label = f"[ẢNH {idx + 1} - CHỦ THỂ SỐ {idx + 1} (SUBJECT {idx + 1})]:"
            elif template_id and len(image_base64_list) == 2:
                # Nhãn cho các template thông thường (1 Chủ thể + 1 Style)
                label = (
                    "[ẢNH 1 - CHỦ THỂ (MAIN SUBJECT)]:"
                    if idx == 0
                    else "[ẢNH 2 - MẪU STYLE (STYLE REFERENCE)]:"
                )
            else:
                # Nhãn mặc định khi khách chat tự do
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

    # ==================== SYSTEM MEMORY ====================
    # Đặt SAU user message, KHÔNG lưu vào DB history.
    # entities thay đổi mỗi lượt — nằm sau prefix nên không phá cache.
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

    # ==================== HEADER & PAYLOAD ====================
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

        # ── Log caching ──
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

        # ── Lưu session: entities + append cặp (user, assistant) vào history ──
        database.update_session_data(
            session_id=session_id,
            response_id=res_data.get("id"),
            entities=ai_json.get("entities_extracted"),
            new_user_msg=user_content,  # list (giữ nguyên format có ảnh)
            new_assistant_text=raw_text,  # chuỗi JSON gốc
        )

        return ai_json

    except Exception as e:
        print(f"Call Conversation API Exception: {e}")
        return None


def _to_base64_url(path: str) -> str | None:
    """Chuyển /uploads/x.jpg hoặc /outputs/x.jpg thành data:image/...;base64,..."""
    if not path:
        return None
    if path.startswith("data:"):
        return path
    if path.startswith("/uploads/") or path.startswith("/outputs/"):
        file_path = os.path.join(_PROJECT_ROOT, path.lstrip("/"))
        if not os.path.exists(file_path):
            return None
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
    """
    Sinh ảnh preview từ AI (luôn trả về bản preview, không generate final).
    - Tạo ảnh từ đầu (is_retry=False): Dùng IMAGE_MODEL_GEN
    - Retry/sửa ảnh  (is_retry=True):  Dùng IMAGE_MODEL_EDIT
    Final được xử lý hoàn toàn riêng qua finalize_image() — không gọi lại AI.
    """
    output_dir = os.path.join(_PROJECT_ROOT, "outputs")
    os.makedirs(output_dir, exist_ok=True)

    pod_output_instruction = """
    CRITICAL PRINT-ON-DEMAND REQUIREMENT:
    - The final graphic MUST be rendered on a PURE SOLID WHITE BACKGROUND or fully transparent.
    - Absolutely NO gradients, NO shadows, NO environment, NO scenic elements.
    - Main subject must be perfectly isolated, centered, and clean for apparel printing.
    """

    if is_retry:
        # Khi retry: KHÔNG thêm lệnh xóa background — khách chỉ muốn sửa 1 chi tiết nhỏ,
        # không muốn AI rebuild lại toàn bộ bố cục hay xóa background đang có.
        full_prompt = master_prompt
    else:
        # Generate lần đầu: append đầy đủ yêu cầu POD output
        full_prompt = master_prompt + "\n\n" + pod_output_instruction

    # ============================================================
    # LUẬT CHỌN MODEL ĐỘNG THEO YÊU CẦU ĐỔI MỚI
    # ============================================================
    selected_model = config.IMAGE_MODEL_EDIT if is_retry else config.IMAGE_MODEL_GEN

    try:
        # 1. CHẾ ĐỘ CÓ ẢNH THAM CHIẾU (Image-to-Image / Multi-Image Editing)
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

        # 2. CHẾ ĐỘ SINH ẢNH MỚI HOÀN TOÀN TỪ CHỮ (Text-to-Image)
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

        if "url" in img_data:
            img_bytes = requests.get(img_data["url"], timeout=60).content
            img_raw = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        elif "b64_json" in img_data:
            img_raw = Image.open(
                io.BytesIO(base64.b64decode(img_data["b64_json"]))
            ).convert("RGBA")
        else:
            raise Exception("No image returned from API")

        print("👕 Compositing generated image onto mockup...")
        # Trả về ảnh gốc nguyên bản chất lượng cao từ Grok
        print("💾 Saving raw image from Grok...")
        filename = f"preview_raw_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        img_raw.save(os.path.join(output_dir, filename), format="PNG")
        return {"image_url": f"/outputs/{filename}", "error": None}
    except Exception as e:
        print(f"❌ call_image_generation Exception: {e}")
        return {"image_url": None, "error": str(e)}


def finalize_image(preview_image_url: str):
    """
    Chốt ảnh final — KHÔNG gọi AI, KHÔNG edit gì thêm.
    Nhận đúng file preview khách đã duyệt (đã tách nền sẵn).
    Chỉ upscale lên 4500x5400 300 DPI bằng Pillow thuần + sharpen để bù mờ.
    """
    from PIL import ImageFilter

    output_dir = os.path.join(_PROJECT_ROOT, "outputs")
    os.makedirs(output_dir, exist_ok=True)

    try:
        # Đọc file preview từ đường dẫn nội bộ — không qua AI, không qua network
        if preview_image_url.startswith("/outputs/"):
            file_path = os.path.join(_PROJECT_ROOT, preview_image_url.lstrip("/"))
        else:
            return {
                "image_url": None,
                "error": "Invalid preview_image_url — must be an /outputs/ path",
            }

        if not os.path.exists(file_path):
            return {"image_url": None, "error": f"Preview file not found: {file_path}"}

        print(f"🚀 Finalizing from preview: {preview_image_url}")

        # Đọc ảnh preview — đã tách nền sẵn từ bước generate, KHÔNG gọi remove_bg lại
        # (gọi lại flood fill trên ảnh đã có alpha sẽ làm hỏng viền chủ thể)
        img = Image.open(file_path).convert("RGBA")

        # ── BƯỚC 1: Tách riêng kênh RGB và Alpha để xử lý độc lập ──────────────
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))

        # ── BƯỚC 2: Upscale RGB lên canvas target bằng LANCZOS ──────────────────
        # Tính kích thước mới giữ nguyên aspect ratio, vừa trong 85% canvas
        target_w, target_h = 4500, 5400
        max_w = int(target_w * 0.85)  # 3825
        max_h = int(target_h * 0.85)  # 4590
        aspect = img.width / img.height
        if aspect > (max_w / max_h):
            new_w = max_w
            new_h = int(max_w / aspect)
        else:
            new_w = int(max_h * aspect)
            new_h = max_h

        rgb_resized = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)
        alpha_resized = a.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # ── BƯỚC 3: Sharpen RGB sau upscale để bù độ mờ do kéo giãn ────────────
        # UnsharpMask: radius=2 là vùng blur so sánh, percent=150 là cường độ,
        # threshold=3 là ngưỡng pixel tối thiểu để áp dụng (tránh noise)
        rgb_sharp = rgb_resized.filter(
            ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)
        )

        # ── BƯỚC 4: Ghép lại RGBA và paste vào canvas trắng trong suốt ──────────
        img_final_subject = Image.merge("RGBA", (*rgb_sharp.split(), alpha_resized))

        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        paste_x = (target_w - new_w) // 2
        paste_y = (target_h - new_h) // 2
        canvas.paste(img_final_subject, (paste_x, paste_y), img_final_subject)

        # ── BƯỚC 5: Lưu file PNG 300 DPI chuẩn xưởng in ────────────────────────
        filename = f"pod_design_final_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        canvas.save(
            os.path.join(output_dir, filename),
            format="PNG",
            dpi=(300, 300),
            optimize=True,
        )
        print(f"✅ Final saved: {filename} ({target_w}x{target_h} @ 300 DPI)")
        return {"image_url": f"/outputs/{filename}", "error": None}

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
    """API trả về danh sách các Template để Frontend render giao diện chọn lựa"""
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
    """API trả về danh sách sản phẩm mockup để Frontend render trang xem thử"""
    return JSONResponse(content=MOCKUPS_DATA)


@app.post("/chat")
async def chat_endpoint(request: Request):
    data = await request.json()
    user_input = data.get("message")
    image_list = data.get("image_base64_list")
    last_gen_url = data.get("last_generated_image_url")
    session_id = data.get("session_id")
    template_id = data.get("template_id")

    # Đảm bảo session_id luôn tồn tại và hợp lệ
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
    """
    Endpoint retry ảnh — đọc lại toàn bộ luật trong SYSTEM_PROMPT trước.
    Flow:
      1. Gửi yêu cầu sửa + ảnh preview hiện tại vào call_conversation_api
         → AI đọc luật, hiểu context, tạo ra master_prompt mới
      2. Dùng master_prompt mới + ảnh preview làm reference → gọi /images/edits
    """
    data = await request.json()
    retry_instruction = data.get("retry_instruction")  # Yêu cầu sửa của khách
    preview_image_url = data.get("preview_image_url")  # URL ảnh preview hiện tại
    session_id = data.get("session_id")
    template_id = data.get("template_id")

    if not retry_instruction:
        return JSONResponse(
            content={"error": "Missing retry_instruction"}, status_code=400
        )
    if not preview_image_url:
        return JSONResponse(
            content={"error": "Missing preview_image_url"}, status_code=400
        )

    # ── BƯỚC 1: Đọc ảnh preview từ disk, convert sang base64 để gửi cho AI ──
    preview_b64 = None
    if preview_image_url.startswith("/outputs/"):
        file_path = os.path.join(_PROJECT_ROOT, preview_image_url.lstrip("/"))
        if os.path.exists(file_path):
            import base64 as _b64

            with open(file_path, "rb") as f:
                preview_b64 = (
                    "data:image/png;base64," + _b64.b64encode(f.read()).decode()
                )

    # ── BƯỚC 2: Gọi conversation API — AI đọc luật + ảnh + yêu cầu sửa
    #    → sinh ra master_prompt mới đã tuân thủ đầy đủ bộ luật ──────────────
    ai_response = call_conversation_api(
        user_message=(
            f"[EDIT REQUEST]: {retry_instruction}\n\n"
            f"[INSTRUCTION]: Based on the current design image I'm sending, "
            f"please apply the edit request above while strictly following ALL design rules. "
            f"Generate a new `master_prompt` in `is_ready_to_render: true` state."
        ),
        session_id=session_id or f"retry_{int(time.time())}",
        image_base64_list=[preview_b64] if preview_b64 else None,
        last_generated_image_url=None,
        template_id=template_id,
    )

    if not ai_response or not ai_response.get("master_prompt"):
        return JSONResponse(
            content={"error": "AI failed to generate a revised master_prompt"},
            status_code=500,
        )

    revised_prompt = ai_response["master_prompt"]
    print(f"✏️ Retry — revised master_prompt từ AI:\n{revised_prompt[:300]}...")

    # ── BƯỚC 3: Gọi image edit với master_prompt mới + ảnh preview làm reference ──
    img_res = call_image_generation(
        master_prompt=revised_prompt,
        reference_images=[preview_image_url],
        is_retry=True,
    )
    return JSONResponse(content=img_res)


@app.post("/finalize-image")
async def finalize_image_endpoint(request: Request):
    """
    Endpoint chốt ảnh final — KHÔNG gọi lại AI.
    Frontend gửi lên đúng preview_image_url của ảnh khách đã duyệt.
    Backend chỉ upscale lên 4500x5400 300 DPI và trả về file chuẩn xưởng in.
    """
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
