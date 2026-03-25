from flask import render_template, request, g
from .decorators import login_required
from . import models

def init_app(bp):
    @bp.route("/")
    @login_required
    def index():
        stats = models.get_fission_stats(g.agent)
        return render_template("dashboard.html", agent=g.agent, stats=stats)

    @bp.route("/members")
    @login_required
    def members():
        page = request.args.get("page", 1, type=int)
        status = request.args.get("status", None)
        search = request.args.get("q", None)
        result = models.get_members_paginated(g.agent, page=page, status_filter=status, search=search)
        return render_template("members.html", agent=g.agent, **result)

    @bp.route("/tree")
    @login_required
    def tree():
        tree_data = models.build_agent_tree(g.agent)
        return render_template("tree.html", agent=g.agent, tree=tree_data)

    @bp.route("/settings")
    @login_required
    def settings():
        return render_template("settings.html", agent=g.agent)

    @bp.route("/codes")
    @login_required
    def codes():
        from .utils import sb
        r = sb().table("custom_referral_codes").select("*") \
            .eq("owner_id", g.agent["agent_id"]).execute()
        custom_codes = r.data or []
        return render_template("codes.html", agent=g.agent, custom_codes=custom_codes)
