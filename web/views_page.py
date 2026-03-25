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

    @bp.route("/settings")
    @login_required
    def settings():
        return render_template("settings.html", agent=g.agent, is_admin=g.is_admin)

    # ── 管理員專用 ──
    @bp.route("/admin/agents")
    @admin_required
    def admin_agents():
        agents_list = sb().table("agents").select("*") \
            .eq("tenant_id", g.agent["tenant_id"]).order("created_at").execute().data or []
        # 計算每個代理的直推下線數
        members = sb().table("members").select("referred_by") \
            .eq("tenant_id", g.agent["tenant_id"]).execute().data or []
        ref_counts = {}
        for m in members:
            rb = m.get("referred_by")
            if rb:
                ref_counts[rb] = ref_counts.get(rb, 0) + 1
        for a in agents_list:
            a["_downline_count"] = ref_counts.get(a["agent_id"], 0)
        return render_template("admin_agents.html", agent=g.agent, is_admin=True, agents=agents_list)

    # 保留 tree 和 codes 路由做重定向，避免 404
    @bp.route("/tree")
    @login_required
    def tree():
        from flask import redirect, url_for
        if g.is_admin:
            return redirect(url_for("dashboard.admin_agents"))
        return redirect(url_for("dashboard.index"))

    @bp.route("/codes")
    @login_required
    def codes():
        from flask import redirect, url_for
        return redirect(url_for("dashboard.settings"))
