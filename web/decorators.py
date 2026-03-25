from functools import wraps
from flask import request, redirect, url_for, g
from .utils import get_session, sb

COOKIE_NAME = "bjzy_session"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get(COOKIE_NAME)
        session = get_session(token)
        if not session:
            return redirect(url_for("dashboard.login"))
        # Load agent into g
        agent_id = session["agent_id"]
        r = sb().table("agents").select("*").eq("agent_id", agent_id).execute()
        if not r.data or not r.data[0].get("is_active"):
            return redirect(url_for("dashboard.login"))
        g.agent = r.data[0]
        g.session = session
        return f(*args, **kwargs)
    return decorated
