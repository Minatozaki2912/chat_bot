# app/database.py
import json

# Dùng dictionary để lưu trạng thái hội thoại trên RAM.
session_db = {}

def get_previous_id(session_id: str):
    """Lấy ID phản hồi mới nhất"""
    return session_db.get(session_id, {}).get("prev_id")

def get_last_entities(session_id: str):
    """Lấy toàn bộ dữ liệu JSON thực thể đã lưu từ lượt trước"""
    return session_db.get(session_id, {}).get("entities")

def get_history(session_id: str) -> list:
    """
    Lấy lịch sử hội thoại đầy đủ (user + assistant messages).
    Trả về list rỗng nếu chưa có lịch sử.
    Đây là nền tảng để Grok cache hit — prefix phải giống hệt lượt trước.
    """
    return session_db.get(session_id, {}).get("history", [])

def update_session_data(session_id: str, response_id: str, entities: dict, new_user_msg, new_assistant_text: str):
    """
    Cập nhật session: prev_id, entities, và append cặp (user, assistant) vào history.

    new_user_msg: có thể là string hoặc list (khi có ảnh) — giữ nguyên format
    new_assistant_text: chuỗi JSON text mà assistant trả về
    """
    existing = session_db.get(session_id, {})
    history = existing.get("history", [])

    # Append cặp user → assistant vào cuối history
    # Giữ nguyên format content (string hoặc array) để Grok nhận đúng
    history.append({"role": "user",      "content": new_user_msg})
    history.append({"role": "assistant", "content": new_assistant_text})

    session_db[session_id] = {
        "prev_id":  response_id,
        "entities": entities,
        "history":  history,
    }

def delete_session(session_id: str):
    """Xóa sạch toàn bộ session để Reset"""
    if session_id in session_db:
        del session_db[session_id]