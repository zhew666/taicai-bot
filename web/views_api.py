from flask import request, jsonify, g
from datetime import datetime, timedelta, timezone
from .decorators import login_required
from .utils import sb, hash_password, check_password
from . import models
import re

def init_app(bp):

    @bp.route("/api/stats")
    @login_required
    def api_stats():
        stats = models.get_fission_stats(g.agent)
        return jsonify(stats)

    @bp.route("/api/members")
    @login_required
    def api_members():
        page = request.args.get("page", 1, type=int)
        status = request.args.get("status", None)
        search = request.args.get("q", None)
        result = models.get_members_paginated(g.agent, page=page, status_filter=status, search=search)
        # Serialize for JSON
        for m in result["members"]:
            for k, v in m.items():
                if isinstance(v, datetime):
                    m[k] = v.isoformat()
        return jsonify(result)

    @bp.route("/api/members/<ref_code>/extend", methods=["POST"])
    @login_required
    def api_extend_member(ref_code):
        days = request.json.get("days", 0) if request.is_json else int(request.form.get("days", 0))
        if days <= 0 or days > g.agent.get("max_extend_days", 31):
            return jsonify({"error": f"天數須在 1~{g.agent.get('max_extend_days', 31)} 之間"}), 400

        # Find target member
        ref_upper = ref_code.strip().upper()
        r = sb().table("members").select("*").eq("referral_code", ref_upper).execute()
        if not r.data:
            return jsonify({"error": "找不到該會員"}), 404
        target = r.data[0]

        # Check target is in agent's downline
        descendants = models.get_all_descendants(g.agent)
        allowed_ids = [g.agent["agent_id"]] + [a["agent_id"] for a in descendants]
        if target.get("referred_by") not in allowed_ids:
            return jsonify({"error": "該會員不在你的下線中"}), 403

        # Extend
        now = datetime.now(timezone.utc)
        current_exp = None
        if target.get("expire_at"):
            try:
                current_exp = datetime.fromisoformat(target["expire_at"].replace("Z", "+00:00"))
            except Exception:
                pass

        base = max(now, current_exp) if current_exp else now
        new_exp = base + timedelta(days=days)

        sb().table("members").update({
            "expire_at": new_exp.isoformat(),
            "is_member": False,
        }).eq("user_id", target["user_id"]).execute()

        # Log action
        sb().table("agent_actions_log").insert({
            "agent_id": g.agent["agent_id"],
            "action": "extend",
            "target_user_id": target["user_id"],
            "details": {"days": days, "new_expire": new_exp.isoformat(), "ref_code": ref_upper},
        }).execute()

        return jsonify({"ok": True, "new_expire": new_exp.isoformat()})

    @bp.route("/api/members/<ref_code>/activate", methods=["POST"])
    @login_required
    def api_activate_member(ref_code):
        ref_upper = ref_code.strip().upper()
        r = sb().table("members").select("*").eq("referral_code", ref_upper).execute()
        if not r.data:
            return jsonify({"error": "找不到該會員"}), 404
        target = r.data[0]

        # Check permission
        descendants = models.get_all_descendants(g.agent)
        allowed_ids = [g.agent["agent_id"]] + [a["agent_id"] for a in descendants]
        if target.get("referred_by") not in allowed_ids:
            return jsonify({"error": "該會員不在你的下線中"}), 403

        sb().table("members").update({
            "is_member": True,
            "expire_at": None,
        }).eq("user_id", target["user_id"]).execute()

        sb().table("agent_actions_log").insert({
            "agent_id": g.agent["agent_id"],
            "action": "activate",
            "target_user_id": target["user_id"],
            "details": {"ref_code": ref_upper},
        }).execute()

        return jsonify({"ok": True})

    @bp.route("/api/tree")
    @login_required
    def api_tree():
        tree_data = models.build_agent_tree(g.agent)
        return jsonify(tree_data)

    @bp.route("/api/codes", methods=["POST"])
    @login_required
    def api_create_code():
        code = (request.json.get("code", "") if request.is_json else request.form.get("code", "")).strip().upper()
        if not code or len(code) < 3 or len(code) > 20:
            return jsonify({"error": "推廣碼長度須 3~20 字元"}), 400
        if not re.match(r'^[A-Z0-9_-]+$', code):
            return jsonify({"error": "推廣碼只能包含英文、數字、底線、連字號"}), 400

        # Check not taken
        existing = sb().table("custom_referral_codes").select("id").eq("code", code).execute()
        if existing.data:
            return jsonify({"error": "此推廣碼已被使用"}), 409

        # Check not conflict with REF-XXXX
        existing2 = sb().table("members").select("user_id").eq("referral_code", code).execute()
        if existing2.data:
            return jsonify({"error": "此推廣碼與系統碼衝突"}), 409

        sb().table("custom_referral_codes").insert({
            "code": code,
            "owner_id": g.agent["agent_id"],
            "tenant_id": g.agent["tenant_id"],
        }).execute()

        # Also update agent's custom_ref_code
        sb().table("agents").update({"custom_ref_code": code}).eq("agent_id", g.agent["agent_id"]).execute()

        return jsonify({"ok": True, "code": code})

    @bp.route("/api/codes/<code>", methods=["DELETE"])
    @login_required
    def api_delete_code(code):
        code_upper = code.strip().upper()
        sb().table("custom_referral_codes").update({"is_active": False}) \
            .eq("code", code_upper).eq("owner_id", g.agent["agent_id"]).execute()
        # Clear from agent if it was the active one
        if g.agent.get("custom_ref_code", "").upper() == code_upper:
            sb().table("agents").update({"custom_ref_code": None}).eq("agent_id", g.agent["agent_id"]).execute()
        return jsonify({"ok": True})

    @bp.route("/api/settings/password", methods=["PUT"])
    @login_required
    def api_change_password():
        data = request.json or {}
        old_pw = data.get("old_password", "")
        new_pw = data.get("new_password", "")
        if not new_pw or len(new_pw) < 6:
            return jsonify({"error": "新密碼至少 6 字元"}), 400
        if g.agent.get("password_hash") and not check_password(old_pw, g.agent["password_hash"]):
            return jsonify({"error": "舊密碼錯誤"}), 403
        sb().table("agents").update({"password_hash": hash_password(new_pw)}).eq("agent_id", g.agent["agent_id"]).execute()
        return jsonify({"ok": True})

    @bp.route("/api/settings/profile", methods=["PUT"])
    @login_required
    def api_update_profile():
        data = request.json or {}
        name = data.get("display_name", "").strip()
        if not name:
            return jsonify({"error": "名稱不可為空"}), 400
        sb().table("agents").update({"display_name": name}).eq("agent_id", g.agent["agent_id"]).execute()
        return jsonify({"ok": True})
