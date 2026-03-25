import bcrypt
import uuid
from datetime import datetime, timedelta, timezone
from supabase import create_client
import os

_sb_url = os.environ.get("SUPABASE_URL", "")
_sb_key = os.environ.get("SUPABASE_KEY", "")
_sb_client = None

def sb():
    global _sb_client
    if _sb_client is None:
        _sb_client = create_client(_sb_url, _sb_key)
    return _sb_client

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def generate_session_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex

SESSION_DAYS = 7

def create_session(agent_id: str) -> str:
    token = generate_session_token()
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    sb().table("agent_sessions").insert({
        "agent_id": agent_id,
        "token": token,
        "expires_at": expires.isoformat(),
    }).execute()
    return token

def get_session(token: str):
    if not token:
        return None
    r = sb().table("agent_sessions").select("*").eq("token", token).execute()
    if not r.data:
        return None
    session = r.data[0]
    exp = datetime.fromisoformat(session["expires_at"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) > exp:
        sb().table("agent_sessions").delete().eq("token", token).execute()
        return None
    return session

def destroy_session(token: str):
    if token:
        sb().table("agent_sessions").delete().eq("token", token).execute()

def get_agent_by_code(code: str):
    """用 agent_code 或 custom_ref_code 找代理"""
    code_upper = code.strip().upper()
    r = sb().table("agents").select("*").eq("agent_code", code_upper).execute()
    if r.data:
        return r.data[0]
    r = sb().table("agents").select("*").eq("custom_ref_code", code_upper).execute()
    if r.data:
        return r.data[0]
    return None
