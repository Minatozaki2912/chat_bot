# app/database.py
import json

# Dùng dictionary để lưu ID và trạng thái hội thoại trên RAM.
session_db = {}

def get_previous_id(session_id: str):
    """Lấy ID phản hồi mới nhất"""
    return session_db.get(session_id, {}).get("prev_id")

def get_last_entities(session_id: str):
    """Lấy toàn bộ dữ liệu JSON thực thể đã lưu từ lượt trước"""
    return session_db.get(session_id, {}).get("entities")

def update_session_data(session_id: str, response_id: str, entities: dict):
    """Cập nhật cả ID mới và Dữ liệu JSON mới nhất vào Database"""
    session_db[session_id] = {
        "prev_id": response_id,
        "entities": entities
    }

def delete_session(session_id: str):
    """Xóa sạch ID cũ để Reset Session"""
    if session_id in session_db:
        del session_db[session_id]