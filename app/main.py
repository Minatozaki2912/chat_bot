# app/main.py
import json
import os
import requests
import time
import io
import base64
from PIL import Image
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from datetime import datetime

import app.config as config
import app.prompts as prompts
import app.database as database

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
    template_id: str = None, # [NEW] Tham số nhận ID Template
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
                label = "[ẢNH 1 - CHỦ THỂ (MAIN SUBJECT)]:" if idx == 0 else "[ẢNH 2 - MẪU STYLE (STYLE REFERENCE)]:"
                user_content.append({"type": "text", "text": label})
            else:
                user_content.append({"type": "text", "text": f"[Ảnh {idx + 1} do khách gửi]:"})
                
            user_content.append({"type": "image_url", "image_url": {"url": valid_b64_url}})

    if last_generated_image_url:
        valid_b64_url = get_valid_image_url(last_generated_image_url)
        user_content.append(
            {
                "type": "text",
                "text": "[SYSTEM]: This is the design you generated in the previous turn. Use this as the primary visual context for requested edits.",
            }
        )
        user_content.append(
            {"type": "image_url", "image_url": {"url": valid_b64_url}}
        )

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

    url = f"{config.BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    # === PAYLOAD CHO GROK-4.3 ===
    payload = {
        "model": config.CONVERSATION_MODEL,
        "messages": messages,
        "previous_response_id": prev_id,
        "temperature": config.TEMPERATURE,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "pod_output",
                "strict": True,
                "schema": prompts.JSON_SCHEMA,
            },
        },
        "store": config.STORE_CONTEXT,
        "reasoning": {"effort": config.REASONING_EFFORT}   # ← Thêm dòng này
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
# =====================================================================
# 2. CỖ MÁY SINH ẢNH (Hỗ trợ Text-to-Image + Single + Multi-Image)
# =====================================================================
def call_image_generation(master_prompt: str, reference_images: list = None):
    headers = {
        "Authorization": f"Bearer {config.XAI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": config.IMAGE_MODEL,
        "prompt": master_prompt,
        "response_format": "url",
    }

    if reference_images and len(reference_images) > 0:
        url = f"{config.BASE_URL}/images/edits"
        images_list = [{"type": "image_url", "url": get_valid_image_url(img)} for img in reference_images if img]
        if len(images_list) == 1:
            payload["image"] = images_list[0]
        else:
            payload["images"] = images_list
    else:
        url = f"{config.BASE_URL}/images/generations"

    try:
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code == 200:
            raw_url = response.json()["data"][0]["url"]

            # === XỬ LÝ ẢNH CHUẨN PNG IN ÁO ===
            img_response = requests.get(raw_url)
            img = Image.open(io.BytesIO(img_response.content)).convert("RGBA")

            # Resize giữ tỷ lệ
            target_w, target_h = 4500, 5400
            aspect = img.width / img.height
            if aspect > (target_w / target_h):
                new_w = target_w
                new_h = int(target_w / aspect)
            else:
                new_h = target_h
                new_w = int(target_h * aspect)

            img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # Tạo canvas trong suốt hoàn toàn
            final_img = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))  # Nền trong suốt hoàn toàn

            # Dán vào giữa
            paste_x = (target_w - new_w) // 2
            paste_y = (target_h - new_h) // 2
            final_img.paste(img_resized, (paste_x, paste_y), img_resized)

            # Lưu PNG chất lượng cao
            filename = f"pod_design_{int(time.time())}.png"
            filepath = os.path.join("outputs", filename)
            
            final_img.save(filepath, format="PNG", dpi=(300, 300), optimize=True)

            return {"image_url": f"/outputs/{filename}", "error": None}

        else:
            return {"image_url": None, "error": response.text[:200]}

    except Exception as e:
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

@app.post("/chat")
async def chat_endpoint(request: Request):
    data = await request.json()
    user_input = data.get("message")
    image_list = data.get("image_base64_list")
    last_gen_url = data.get("last_generated_image_url")
    session_id = data.get("session_id", "user_default")
    template_id = data.get("template_id") # [NEW]

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
    reference_images = data.get("reference_images")   # ← Đổi thành list

    if not master_prompt:
        return JSONResponse(
            content={"error": "Missing master_prompt"}, 
            status_code=400
        )

    img_res = call_image_generation(master_prompt, reference_images)
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
