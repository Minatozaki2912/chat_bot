import os
from dotenv import load_dotenv

load_dotenv()

# ====================== xAI (Grok) ======================
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_BASE_URL = "https://api.x.ai/v1"

# ====================== OpenAI ======================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = "https://api.openai.com/v1"

# ====================== MODELS ======================
CONVERSATION_MODEL = "grok-4.20-0309-non-reasoning"

IMAGE_MODEL_GPT = "gpt-image-2"           # Generate chính
IMAGE_MODEL_GROK = "grok-imagine-image"   # Dùng cho Retry/Edit

# ====================== CÁC THAM SỐ CŨ (BẮT BUỘC PHẢI CÓ) ======================
BASE_URL = XAI_BASE_URL                   # Dùng cho chat
CHAT_TEMPERATURE = 0.7
IMAGE_TEMPERATURE = 0.4
STORE_CONTEXT = True
REASONING_EFFORT = "none"

print("✅ Config loaded successfully!")