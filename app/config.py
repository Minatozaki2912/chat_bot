# ai/config.py
import os
from dotenv import load_dotenv

load_dotenv()

# ==============================================
# xAI (Grok)
# ==============================================
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_BASE_URL = "https://api.x.ai/v1"

BASE_URL = XAI_BASE_URL

SITE_URL = os.getenv("SITE_URL", "https://vibetobox.com")

# ==============================================
# MODELS
# ==============================================
CONVERSATION_MODEL = "grok-4.20-0309-non-reasoning"

IMAGE_MODEL_GEN  = "grok-imagine-image-quality"  # Khi sinh ảnh mới hoàn toàn
IMAGE_MODEL_EDIT = "grok-imagine-image"          # Khi sửa ảnh (Image-to-Image)

# ==============================================
# THAM SỐ
# ==============================================
CHAT_TEMPERATURE  = 0.7
IMAGE_TEMPERATURE = 0.6
STORE_CONTEXT     = True
REASONING_EFFORT  = "none"