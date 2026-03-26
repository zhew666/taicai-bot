from flask import request, redirect, url_for, render_template, make_response, flash
from .utils import get_agent_by_code, check_password, create_session, destroy_session
from .decorators import COOKIE_NAME

def init_app(bp):
    @bp.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            code = request.form.get("code", "").strip()
            password = request.form.get("password", "").strip()
            if not code or not password:
                error = "請輸入推廣碼和密碼"
            else:
                agent = get_agent_by_code(code)
                if not agent:
                    error = "推廣碼不存在"
                elif not agent.get("password_hash"):
                    error = "帳號尚未設定密碼，請聯繫管理員"
                elif not agent.get("is_active"):
                    error = "帳號已停用"
                elif not check_password(password, agent["password_hash"]):
                    error = "密碼錯誤"
                else:
                    token = create_session(agent["agent_id"], agent.get("tenant_id", ""))
                    resp = make_response(redirect(url_for("dashboard.index")))
                    resp.set_cookie(COOKIE_NAME, token, max_age=7*86400, httponly=True, samesite="Lax", secure=True)
                    return resp
        return render_template("login.html", error=error)

    @bp.route("/logout")
    def logout():
        token = request.cookies.get(COOKIE_NAME)
        destroy_session(token)
        resp = make_response(redirect(url_for("dashboard.login")))
        resp.delete_cookie(COOKIE_NAME)
        return resp
