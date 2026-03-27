from flask import request, jsonify, g
from datetime import datetime, timedelta, timezone
from .decorators import login_required, admin_required
from .utils import sb, hash_password, check_password
from . import models
import re, uuid

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
        r = sb().table("members").select("*").eq("referral_code", ref_upper).eq("tenant_id", g.agent["tenant_id"]).execute()
        if not r.data:
            return jsonify({"error": "找不到該會員"}), 404
        target = r.data[0]

        # Check permission (admin skip)
        if not g.agent.get("is_admin"):
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
        }).eq("user_id", target["user_id"]).eq("tenant_id", g.agent["tenant_id"]).execute()

        # Log action
        sb().table("agent_actions_log").insert({
            "agent_id": g.agent["agent_id"],
            "tenant_id": g.agent["tenant_id"],
            "action": "extend",
            "target_user_id": target["user_id"],
            "details": {"days": days, "new_expire": new_exp.isoformat(), "ref_code": ref_upper},
        }).execute()

        return jsonify({"ok": True, "new_expire": new_exp.isoformat()})

    @bp.route("/api/members/<ref_code>/set-expire", methods=["POST"])
    @login_required
    def api_set_expire(ref_code):
        data = request.json or {}
        expire_at = data.get("expire_at", "")
        if not expire_at:
            return jsonify({"error": "請提供到期時間"}), 400

        try:
            new_exp = datetime.fromisoformat(expire_at.replace("Z", "+00:00"))
        except Exception:
            return jsonify({"error": "時間格式錯誤"}), 400

        ref_upper = ref_code.strip().upper()
        r = sb().table("members").select("*").eq("referral_code", ref_upper).eq("tenant_id", g.agent["tenant_id"]).execute()
        if not r.data:
            return jsonify({"error": "找不到該會員"}), 404
        target = r.data[0]

        if not g.agent.get("is_admin"):
            descendants = models.get_all_descendants(g.agent)
            allowed_ids = [g.agent["agent_id"]] + [a["agent_id"] for a in descendants]
            if target.get("referred_by") not in allowed_ids:
                return jsonify({"error": "該會員不在你的下線中"}), 403

        sb().table("members").update({
            "expire_at": new_exp.isoformat(),
        }).eq("user_id", target["user_id"]).eq("tenant_id", g.agent["tenant_id"]).execute()

        sb().table("agent_actions_log").insert({
            "agent_id": g.agent["agent_id"],
            "tenant_id": g.agent["tenant_id"],
            "action": "set_expire",
            "target_user_id": target["user_id"],
            "details": {"new_expire": new_exp.isoformat(), "ref_code": ref_upper},
        }).execute()

        return jsonify({"ok": True, "new_expire": new_exp.isoformat()})

    @bp.route("/api/members/<ref_code>/activate", methods=["POST"])
    @login_required
    def api_activate_member(ref_code):
        ref_upper = ref_code.strip().upper()
        r = sb().table("members").select("*").eq("referral_code", ref_upper).eq("tenant_id", g.agent["tenant_id"]).execute()
        if not r.data:
            return jsonify({"error": "找不到該會員"}), 404
        target = r.data[0]

        # Check permission (admin skip)
        if not g.agent.get("is_admin"):
            descendants = models.get_all_descendants(g.agent)
            allowed_ids = [g.agent["agent_id"]] + [a["agent_id"] for a in descendants]
            if target.get("referred_by") not in allowed_ids:
                return jsonify({"error": "該會員不在你的下線中"}), 403

        sb().table("members").update({
            "is_member": True,
            "expire_at": None,
        }).eq("user_id", target["user_id"]).eq("tenant_id", g.agent["tenant_id"]).execute()

        sb().table("agent_actions_log").insert({
            "agent_id": g.agent["agent_id"],
            "tenant_id": g.agent["tenant_id"],
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

    # ── 管理員 API ──

    @bp.route("/api/admin/agents", methods=["GET"])
    @admin_required
    def api_admin_agents():
        agents = sb().table("agents").select("*").eq("tenant_id", g.agent["tenant_id"]).order("created_at").execute().data or []
        for a in agents:
            a.pop("password_hash", None)
        return jsonify(agents)

    @bp.route("/api/admin/agents", methods=["POST"])
    @admin_required
    def api_admin_create_agent():
        data = request.json or {}
        display_name = data.get("display_name", "").strip()
        custom_code = data.get("custom_ref_code", "").strip().upper()
        grant_hours = data.get("grant_hours", 6)
        max_extend = data.get("max_extend_days", 31)
        password = data.get("password", "123456")

        if not display_name:
            return jsonify({"error": "名稱不可為空"}), 400
        if custom_code:
            if len(custom_code) < 3 or len(custom_code) > 20:
                return jsonify({"error": "推廣碼長度須 3~20 字元"}), 400
            existing = sb().table("agents").select("agent_id").eq("custom_ref_code", custom_code).eq("tenant_id", g.agent["tenant_id"]).execute()
            if existing.data:
                return jsonify({"error": "推廣碼已被使用"}), 409

        agent_id = str(uuid.uuid4())
        agent_code = f"AGENT-{custom_code}" if custom_code else f"AGENT-{uuid.uuid4().hex[:6].upper()}"
        # 自動掛到當前管理員下
        parent_id = g.agent["agent_id"]
        parent_path = g.agent.get("path", f"/{parent_id}/")
        new_path = f"{parent_path}{agent_id}/"
        new_depth = (g.agent.get("depth") or 0) + 1

        sb().table("agents").insert({
            "agent_id": agent_id,
            "agent_code": agent_code,
            "level": new_depth,
            "parent_agent_id": parent_id,
            "name": display_name,
            "display_name": display_name,
            "max_extend_days": max_extend,
            "is_active": True,
            "tenant_id": g.agent["tenant_id"],
            "path": new_path,
            "depth": new_depth,
            "custom_ref_code": custom_code or None,
            "grant_hours": grant_hours,
            "password_hash": hash_password(password),
        }).execute()

        return jsonify({"ok": True, "agent_id": agent_id, "agent_code": agent_code})

    @bp.route("/api/admin/agents/<agent_id>", methods=["PUT"])
    @admin_required
    def api_admin_update_agent(agent_id):
        data = request.json or {}
        updates = {}

        if "display_name" in data:
            updates["display_name"] = data["display_name"].strip()
        if "custom_ref_code" in data:
            code = data["custom_ref_code"].strip().upper()
            if code:
                existing = sb().table("agents").select("agent_id").eq("custom_ref_code", code).neq("agent_id", agent_id).eq("tenant_id", g.agent["tenant_id"]).execute()
                if existing.data:
                    return jsonify({"error": "推廣碼已被使用"}), 409
            updates["custom_ref_code"] = code or None
        if "grant_hours" in data:
            h = int(data["grant_hours"])
            if h < 0 or h > 8760:
                return jsonify({"error": "贈送時間須 0~8760 小時"}), 400
            updates["grant_hours"] = h
        if "max_extend_days" in data:
            updates["max_extend_days"] = int(data["max_extend_days"])
        if "is_active" in data:
            updates["is_active"] = bool(data["is_active"])
        if "password" in data and data["password"]:
            updates["password_hash"] = hash_password(data["password"])

        if not updates:
            return jsonify({"error": "沒有要更新的欄位"}), 400

        sb().table("agents").update(updates).eq("agent_id", agent_id).eq("tenant_id", g.agent["tenant_id"]).execute()
        return jsonify({"ok": True})

    # ── 試用重置 ──
    @bp.route("/api/members/<ref_code>/reset-trial", methods=["POST"])
    @login_required
    def api_reset_trial(ref_code):
        data = request.json or {}
        hours = data.get("hours", 1)
        if hours <= 0 or hours > 24:
            return jsonify({"error": "時數須在 1~24 之間"}), 400

        ref_upper = ref_code.strip().upper()
        r = sb().table("members").select("*").eq("referral_code", ref_upper).eq("tenant_id", g.agent["tenant_id"]).execute()
        if not r.data:
            return jsonify({"error": "找不到該會員"}), 404
        target = r.data[0]

        if not g.agent.get("is_admin"):
            descendants = models.get_all_descendants(g.agent)
            allowed_ids = [g.agent["agent_id"]] + [a["agent_id"] for a in descendants]
            if target.get("referred_by") not in allowed_ids:
                return jsonify({"error": "該會員不在你的下線中"}), 403

        now = datetime.now(timezone.utc)
        new_exp = now + timedelta(hours=hours)

        sb().table("members").update({
            "trial_start": now.isoformat(),
            "expire_at": new_exp.isoformat(),
            "is_member": False,
        }).eq("user_id", target["user_id"]).eq("tenant_id", g.agent["tenant_id"]).execute()

        sb().table("agent_actions_log").insert({
            "agent_id": g.agent["agent_id"],
            "tenant_id": g.agent["tenant_id"],
            "action": "reset_trial",
            "target_user_id": target["user_id"],
            "details": {"hours": hours, "new_expire": new_exp.isoformat(), "ref_code": ref_upper},
        }).execute()

        return jsonify({"ok": True, "new_expire": new_exp.isoformat()})

    # ── 數據看板（含今日新增） ──
    @bp.route("/api/dashboard-stats")
    @login_required
    def api_dashboard_stats():
        tid = g.agent["tenant_id"]
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        all_members = sb().table("members").select("*").eq("tenant_id", tid).execute().data or []

        total = len(all_members)
        active = 0
        expired = 0
        trial = 0
        new_today = 0

        for m in all_members:
            status = models.classify_member(m)
            if status in ("active", "permanent"):
                active += 1
            elif status == "trial":
                trial += 1
            elif status == "expired":
                expired += 1

            # 今日新增：trial_start 在今天
            ts = m.get("trial_start")
            if ts:
                try:
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if ts_dt >= today_start:
                        new_today += 1
                except Exception:
                    pass

        return jsonify({
            "total": total,
            "active": active,
            "trial": trial,
            "expired": expired,
            "new_today": new_today,
        })

    # ── 系統設定（管理員） ──
    @bp.route("/api/admin/config", methods=["GET"])
    @admin_required
    def api_get_config():
        tid = g.agent["tenant_id"]
        r = sb().table("system_config").select("*").eq("tenant_id", tid).execute()
        config = {}
        for row in (r.data or []):
            config[row["key"]] = row["value"]
        return jsonify(config)

    @bp.route("/api/admin/config", methods=["PUT"])
    @admin_required
    def api_update_config():
        tid = g.agent["tenant_id"]
        data = request.json or {}

        for key, value in data.items():
            existing = sb().table("system_config").select("id").eq("key", key).eq("tenant_id", tid).execute()
            if existing.data:
                sb().table("system_config").update({"value": str(value)}).eq("key", key).eq("tenant_id", tid).execute()
            else:
                sb().table("system_config").insert({"key": key, "value": str(value), "tenant_id": tid}).execute()

        return jsonify({"ok": True})
