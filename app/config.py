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
CONVERSATION_MODEL = "grok-4.20-0309-non-reasoning"                    # Flagship model - Khuyến nghị dùng cho tất cả

# Model sinh ảnh (Quality mode - khuyến nghị cao nhất hiện tại)
IMAGE_MODEL = "grok-imagine-image"

# Tham số chung
CHAT_TEMPERATURE = 0.7                            # 0.7 giúp AI nảy số ý tưởng đa dạng, linh hoạt hơn
IMAGE_TEMPERATURE = 0.4                           # 0.4 giúp AI vẽ bám sát Master Prompt, ít chế bậy
STORE_CONTEXT = True                              # Lưu session (state-first)
# === REASONING EFFORT (Rất quan trọng với grok-4.3) ===
# Bạn có thể override khi gọi API tùy theo nhu cầu
REASONING_EFFORT = "none"      # Options: "none", "low" (default), "medium", "high"