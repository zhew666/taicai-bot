import os
from functools import wraps
from flask import request, redirect, url_for, g, abort
from .utils import get_session, sb

COOKIE_NAME = "bjzy_session"

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get(COOKIE_NAME)
        session = get_session(token)
        if not session:
            return redirect(url_for("dashboard.login"))
        agent_id = session["agent_id"]
        r = sb().table("agents").select("*").eq("agent_id", agent_id).execute()
        if not r.data or not r.data[0].get("is_active"):
            return redirect(url_for("dashboard.login"))
        g.agent = r.data[0]
        g.session = session
        g.is_admin = bool(g.agent.get("is_admin"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not g.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated
