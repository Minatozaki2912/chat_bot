import os
from dotenv import load_dotenv

# Nạp biến môi trường
load_dotenv()

# ==============================================
# THÔNG TIN API xAI (Cập nhật tháng 5/2026)
# ==============================================
XAI_API_KEY = os.getenv("XAI_API_KEY")
BASE_URL = "https://api.x.ai/v1"

# === MODEL MỚI NHẤT ===
CONVERSATION_MODEL = "grok-4.3"                    # Flagship model - Khuyến nghị dùng cho tất cả

# Model sinh ảnh (Quality mode - khuyến nghị cao nhất hiện tại)
IMAGE_MODEL = "grok-imagine-image-quality"

# Tham số chung
TEMPERATURE = 0.6                                 # Giảm xuống 0.4 cho output ổn định hơn khi dùng JSON schema
STORE_CONTEXT = True                               # Lưu session (state-first)

# === REASONING EFFORT (Rất quan trọng với grok-4.3) ===
# Bạn có thể override khi gọi API tùy theo nhu cầu
REASONING_EFFORT = "none"      # Options: "none", "low" (default), "medium", "high"