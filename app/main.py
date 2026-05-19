import json
import os
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
import numpy as np
import app.config as config
import app.database as database
import app.prompts as prompts

# === THÊM OpenAI Client ===
from openai import OpenAI

# Khởi tạo OpenAI client
openai_client = OpenAI(api_key=config.OPENAI_API_KEY)

app = FastAPI()
templates = Jinja2Templates(directory="templates")

os.makedirs("outputs", exist_ok=True)
app.mount("/outputs", StaticFiles(directory="outputs"), name="outputs")


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
# 1. BỘ NÃO ĐIỀU PHỐI (CONVERSATION API - STATEFUL & MULTIMODAL)
# =====================================================================
def call_conversation_api(
    user_message: str,
    session_id: str,
    image_base64_list: list = None,
    last_generated_image_url: str = None,
    template_id: str = None,  # [NEW] Tham số nhận ID Template
):
    prev_id = database.get_previous_id(session_id)
    last_entities = database.get_last_entities(session_id)

    state_instruction = ""
    if last_entities and any(v is not None for v in last_entities.values()):
        entities_str = json.dumps(last_entities, ensure_ascii=False)
        state_instruction = f"\n\n[SYSTEM]: Previously confirmed data (Memory): {entities_str}. You MUST maintain these non-null values in your new JSON response."

    user_content = []
    text_to_send = (user_message if user_message else "") + state_instruction

    if text_to_send:
        user_content.append({"type": "text", "text": text_to_send})

    # XỬ LÝ ĐỌC ĐA ẢNH CHUẨN XÁC CHO AI
    if image_base64_list and isinstance(image_base64_list, list):
        for idx, img_b64 in enumerate(image_base64_list):
            valid_b64_url = get_valid_image_url(img_b64)

            # Nếu đang dùng Template và có 2 ảnh, gán nhãn cứng để AI phân biệt
            if template_id and len(image_base64_list) == 2:
                label = (
                    "[ẢNH 1 - CHỦ THỂ (MAIN SUBJECT)]:"
                    if idx == 0
                    else "[ẢNH 2 - MẪU STYLE (STYLE REFERENCE)]:"
                )
                user_content.append({"type": "text", "text": label})
            else:
                user_content.append(
                    {"type": "text", "text": f"[Ảnh {idx + 1} do khách gửi]:"}
                )

            user_content.append(
                {"type": "image_url", "image_url": {"url": valid_b64_url}}
            )

    if last_generated_image_url:
        valid_b64_url = get_valid_image_url(last_generated_image_url)
        user_content.append(
            {
                "type": "text",
                "text": "[SYSTEM]: This is the design you generated in the previous turn. Use this as the primary visual context for requested edits.",
            }
        )
        user_content.append({"type": "image_url", "image_url": {"url": valid_b64_url}})

    current_time = datetime.now().strftime("%A, %B %d, %Y, %H:%M:%S")
    # Bơm luật Template vào nếu khách có chọn
    template_instruction = ""
    if template_id and template_id in TEMPLATES_DATA:
        rule = TEMPLATES_DATA[template_id].get("master_prompt_rule", "")
        template_instruction = f"\n\n### [ACTIVE TEMPLATE RULE - BẮT BUỘC TUÂN THỦ]\nKhách hàng đã chọn Template: {template_id}. Hãy copy Y NGUYÊN câu luật sau đây vào mục [TEMPLATE RULE] trong công thức Master Prompt:\n{rule}"

    rag_instruction = f"""

### [SYSTEM DATA - MANDATORY READ]
- Current System Time: {current_time}
- Use this time to calculate delivery dates based on the RAG policies below.

### [KNOWLEDGE BASE - RAG POLICIES & PRODUCTS]
{KNOWLEDGE_DATA}
{template_instruction} 
"""
    final_system_prompt = prompts.SYSTEM_PROMPT + rag_instruction

    messages = [
        {"role": "system", "content": final_system_prompt},
        {"role": "user", "content": user_content},
    ]

    url = f"{config.XAI_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    # === PAYLOAD CHO GROK-4.3 ===
    payload = {
        "model": config.CONVERSATION_MODEL,
        "messages": messages,
        "previous_response_id": prev_id,
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

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            print(f"Chat API Error: {response.text}")
            return None

        res_data = response.json()
        ai_json = json.loads(res_data["choices"][0]["message"]["content"])

        database.update_session_data(
            session_id, res_data.get("id"), ai_json.get("entities_extracted")
        )

        return ai_json
    except Exception as e:
        print(f"Chat API Exception: {e}")
        return None


def remove_bg_floodfill(img):
    """
    Hàm gọt nền kết hợp 2 lớp:
    1. Flood Fill: Quét sạch mảng nền lớn bên ngoài (xử lý cả viền mờ).
    2. Numpy Mask: Đục thủng các "lỗ" màu trắng tinh (255,255,255) bị kẹt bên trong.
    """
    img = img.convert("RGBA")

    # --- LỚP 1: Xóa mảng nền bên ngoài bằng Flood Fill ---
    temp = img.convert("RGB")
    magic_color = (255, 0, 255)  # Màu hồng cánh sen làm mốc
    ImageDraw.floodfill(temp, xy=(0, 0), value=magic_color, thresh=30)

    # Chuyển ảnh thành mảng Numpy để xử lý tốc độ cao
    img_array = np.array(img)
    temp_array = np.array(temp)

    # Tạo bộ lọc (mask) nhận diện những vùng bị loang màu hồng (Nền ngoài)
    outer_bg_mask = (
        (temp_array[:, :, 0] == 255)
        & (temp_array[:, :, 1] == 0)
        & (temp_array[:, :, 2] == 255)
    )

    # --- LỚP 2: Đục lỗ bên trong (Bắt các khoảng trắng tinh bị kẹt) ---
    # Chỉ tìm những pixel trắng tuyệt đối (255, 255, 255) do AI tạo ra
    pure_white_mask = (
        (img_array[:, :, 0] >= 250)
        & (img_array[:, :, 1] >= 250)
        & (img_array[:, :, 2] >= 250)
    )

    # --- TỔNG HỢP & LÀM TRONG SUỐT ---
    # Điểm nào thuộc nền ngoài HOẶC là lỗ trắng bên trong -> Đánh thủng (Alpha = 0)
    img_array[outer_bg_mask | pure_white_mask, 3] = 0

    return Image.fromarray(img_array)

def call_image_generation(
    master_prompt: str,
    reference_images: list = None,
    is_final: bool = False,
    is_retry: bool = False,
):
    """
    Hybrid Image Generation:
    - GPT-Image-2:
        + Text-to-image
        + Image-to-image
    - Grok:
        + Retry/Edit mode
    """

    pod_output_instruction = """
    CRITICAL PRINT-ON-DEMAND REQUIREMENT:
    - The final graphic MUST be rendered on a PURE SOLID WHITE BACKGROUND or fully transparent.
    - Absolutely NO gradients, NO shadows, NO environment, NO scenic elements.
    - Main subject must be perfectly isolated, centered, and clean for apparel printing.
    """

    # =========================================================
    # FINAL PROMPT
    # =========================================================

    if is_final:
        sharpness_boost = """
        [FINAL RENDER QUALITY]:
        8k resolution,
        ultra-detailed,
        highly crisp lines,
        vector-like sharpness,
        masterpiece,
        professional print-ready quality.
        """

        full_prompt = (
            master_prompt + "\n\n" + sharpness_boost + "\n\n" + pod_output_instruction
        )
    else:
        full_prompt = master_prompt + "\n\n" + pod_output_instruction

    try:

        # =========================================================
        # RETRY MODE → GROK (Image-to-Image)
        # =========================================================
        if is_retry and reference_images and len(reference_images) > 0:
            print("🔄 Using Grok Imagine for Retry/Edit (Image-to-Image)...")

            # 1. QUAY LẠI DÙNG JSON: Bắt buộc phải khai báo Content-Type
            headers = {
                "Authorization": f"Bearer {config.XAI_API_KEY}",
                "Content-Type": "application/json",
            }

            url = f"{config.XAI_BASE_URL}/images/edits"

            # 2. KHÔNG CẮT CHUỖI NỮA: Lấy nguyên bản chuỗi Base64 
            # (Phải giữ lại đoạn tiền tố "data:image/png;base64,")
            image_input = get_valid_image_url(reference_images[0])

            # 3. ĐÓNG GÓI CHUẨN xAI: Biến `image` là một Object chứa `url`
            payload = {
                "model": config.IMAGE_MODEL_GROK,
                "prompt": full_prompt,
                "n": 1,
                "image": {
                    "url": image_input
                }
            }
            
            # 4. GỬI BẰNG json=payload
            response = requests.post(url, headers=headers, json=payload)

        # =========================================================
        # GPT-IMAGE-2
        # =========================================================

        else:

            print("🎨 Using GPT-Image-2...")

            # =====================================================
            # IMAGE TO IMAGE
            # =====================================================

            if reference_images and len(reference_images) > 0:

                print("🖼️ Mode: IMAGE TO IMAGE")

                url = f"{config.OPENAI_BASE_URL}/images/edits"

                headers = {
                    "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                }

                image_input = get_valid_image_url(reference_images[0])

                # =====================================================
                # BASE64 IMAGE
                # =====================================================

                if image_input.startswith("data:image"):

                    image_b64 = image_input.split(",")[1]

                    image_bytes = base64.b64decode(image_b64)

                # =====================================================
                # IMAGE URL
                # =====================================================

                elif image_input.startswith("http"):

                    img_response = requests.get(
                        image_input,
                        timeout=60,
                    )

                    image_bytes = img_response.content

                # =====================================================
                # INVALID
                # =====================================================

                else:
                    raise Exception(
                        "Unsupported image format"
                    )

                files = {
                    "image": (
                        "reference.png",
                        image_bytes,
                        "image/png",
                    )
                }

                data = {
                    "model": config.IMAGE_MODEL_GPT,
                    "prompt": full_prompt,
                    "size": "1024x1024",
                }

                response = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=180,
                )

            # =====================================================
            # TEXT TO IMAGE
            # =====================================================

            else:

                print("✨ Mode: TEXT TO IMAGE")

                url = f"{config.OPENAI_BASE_URL}/images/generations"

                headers = {
                    "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                }

                payload = {
                    "model": config.IMAGE_MODEL_GPT,
                    "prompt": full_prompt,
                    "size": "1024x1024",
                    "quality": "medium",
                }

                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=180,
                )

        # =========================================================
        # CHECK ERROR
        # =========================================================

        if response.status_code != 200:

            error_msg = response.text[:1000]

            print(f"❌ Image API Error ({response.status_code}):\n{error_msg}")

            return {
                "image_url": None,
                "error": error_msg,
            }

        # =========================================================
        # PARSE RESPONSE
        # =========================================================

        data = response.json()

        print("✅ Image API Success")

        img_data = data["data"][0]

        # =========================================================
        # URL RESPONSE
        # =========================================================

        if "url" in img_data:

            raw_url = img_data["url"]

            img_response = requests.get(
                raw_url,
                timeout=60,
            )

            img_raw = Image.open(io.BytesIO(img_response.content)).convert("RGBA")

        # =========================================================
        # BASE64 RESPONSE
        # =========================================================

        elif "b64_json" in img_data:

            img_bytes = base64.b64decode(img_data["b64_json"])

            img_raw = Image.open(io.BytesIO(img_bytes)).convert("RGBA")

        else:
            raise Exception("No image returned from API")

        # =========================================================
        # PREVIEW MODE
        # =========================================================

        if not is_final:

            print("🧼 Processing preview image...")

            img_no_bg = remove_bg_floodfill(img_raw)

            filename = f"preview_no_bg_{int(time.time())}.png"

            filepath = os.path.join(
                "outputs",
                filename,
            )

            img_no_bg.save(
                filepath,
                format="PNG",
            )

            return {
                "image_url": f"/outputs/{filename}",
                "error": None,
            }

        # =========================================================
        # FINAL POD RENDER
        # =========================================================

        else:

            print("🚀 Processing FINAL POD render...")

            img_no_bg = remove_bg_floodfill(img_raw)

            target_w = 4500
            target_h = 5400

            final_img = Image.new(
                "RGBA",
                (target_w, target_h),
                (0, 0, 0, 0),
            )

            max_w = int(target_w * 0.85)
            max_h = int(target_h * 0.85)

            aspect = img_no_bg.width / img_no_bg.height

            if aspect > (max_w / max_h):

                new_w = max_w
                new_h = int(max_w / aspect)

            else:

                new_h = max_h
                new_w = int(max_h * aspect)

            img_resized = img_no_bg.resize(
                (new_w, new_h),
                Image.Resampling.LANCZOS,
            )

            paste_x = (target_w - new_w) // 2
            paste_y = (target_h - new_h) // 2

            final_img.paste(
                img_resized,
                (paste_x, paste_y),
                img_resized,
            )

            filename = f"pod_design_final_{int(time.time())}.png"

            filepath = os.path.join(
                "outputs",
                filename,
            )

            final_img.save(
                filepath,
                format="PNG",
                dpi=(300, 300),
                optimize=True,
            )

            return {
                "image_url": f"/outputs/{filename}",
                "error": None,
            }

    except Exception as e:

        print(f"❌ call_image_generation Exception: {e}")

        return {
            "image_url": None,
            "error": str(e),
        }


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


@app.post("/chat")
async def chat_endpoint(request: Request):
    data = await request.json()
    user_input = data.get("message")
    image_list = data.get("image_base64_list")
    last_gen_url = data.get("last_generated_image_url")
    session_id = data.get("session_id", "user_default")
    template_id = data.get("template_id")  # [NEW]

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
    is_final = data.get("is_final", False)
    is_retry = data.get("is_retry", False)  # ← Flag quan trọng

    if not master_prompt:
        return JSONResponse(content={"error": "Missing master_prompt"}, status_code=400)

    img_res = call_image_generation(
        master_prompt=master_prompt,
        reference_images=reference_images,
        is_final=is_final,
        is_retry=is_retry,
    )
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
