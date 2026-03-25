from flask import render_template, request, g
from .decorators import login_required, admin_required
from .utils import sb
from . import models

def init_app(bp):
    @bp.route("/")
    @login_required
    def index():
        stats = models.get_fission_stats(g.agent)
        return render_template("dashboard.html", agent=g.agent, stats=stats, is_admin=g.is_admin)

    @bp.route("/members")
    @login_required
    def members():
        page = request.args.get("page", 1, type=int)
        status = request.args.get("status", None)
        search = request.args.get("q", None)
        result = models.get_members_paginated(g.agent, page=page, status_filter=status, search=search)
        return render_template("members.html", agent=g.agent, is_admin=g.is_admin, **result)

    @bp.route("/tree")
    @login_required
    def tree():
        return render_template("tree.html", agent=g.agent, is_admin=g.is_admin)

    @bp.route("/settings")
    @login_required
    def settings():
        return render_template("settings.html", agent=g.agent, is_admin=g.is_admin)

    @bp.route("/codes")
    @login_required
    def codes():
        r = sb().table("custom_referral_codes").select("*") \
            .eq("owner_id", g.agent["agent_id"]).execute()
        custom_codes = r.data or []
        return render_template("codes.html", agent=g.agent, is_admin=g.is_admin, custom_codes=custom_codes)

    # ── 管理員專用頁面 ──
    @bp.route("/admin/agents")
    @admin_required
    def admin_agents():
        agents = sb().table("agents").select("*").eq("tenant_id", g.agent["tenant_id"]).order("created_at").execute().data or []
        return render_template("admin_agents.html", agent=g.agent, is_admin=True, agents=agents)
