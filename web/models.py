from datetime import datetime, timezone, timedelta
from .utils import sb

def get_all_descendants(agent):
    """取得代理所有下線代理（不含自己）"""
    path = agent["path"]
    r = sb().table("agents").select("*") \
        .like("path", f"{path}%") \
        .neq("agent_id", agent["agent_id"]) \
        .eq("tenant_id", agent["tenant_id"]) \
        .execute()
    return r.data or []

def get_direct_children(agent):
    """取得直屬子代理"""
    r = sb().table("agents").select("*") \
        .eq("parent_agent_id", agent["agent_id"]) \
        .eq("tenant_id", agent["tenant_id"]) \
        .execute()
    return r.data or []

def get_downline_members(agent, agent_ids=None):
    """取得代理體系下所有會員（admin 看全部）"""
    if agent.get("is_admin"):
        r = sb().table("members").select("*") \
            .eq("tenant_id", agent["tenant_id"]) \
            .execute()
        return r.data or []
    if agent_ids is None:
        descendants = get_all_descendants(agent)
        agent_ids = [agent["agent_id"]] + [a["agent_id"] for a in descendants]
    r = sb().table("members").select("*") \
        .in_("referred_by", agent_ids) \
        .eq("tenant_id", agent["tenant_id"]) \
        .execute()
    return r.data or []

def classify_member(m):
    """判斷會員狀態：permanent / active / trial / expired / new"""
    now = datetime.now(timezone.utc)
    if m.get("is_member") and not m.get("expire_at"):
        return "permanent"
    if m.get("expire_at"):
        try:
            exp = datetime.fromisoformat(m["expire_at"].replace("Z", "+00:00"))
            if exp > now:
                # 有 trial_start 但沒被開通過 → 還是試用；否則是正式
                if not m.get("is_member") and m.get("trial_start"):
                    ts = datetime.fromisoformat(m["trial_start"].replace("Z", "+00:00"))
                    # 試用期 = trial_start 後 24 小時內
                    if (exp - ts).total_seconds() <= 86400:
                        return "trial"
                return "active"
            else:
                return "expired"
        except Exception:
            return "expired"
    if m.get("trial_start"):
        return "expired"
    return "new"

def get_fission_stats(agent):
    """裂變統計"""
    descendants = get_all_descendants(agent)
    agent_ids = [agent["agent_id"]] + [a["agent_id"] for a in descendants]
    members = get_downline_members(agent, agent_ids)

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    stats = {"total": 0, "active": 0, "trial": 0, "expired": 0, "new": 0, "permanent": 0, "sub_agents": len(descendants), "new_today": 0}

    for m in members:
        stats["total"] += 1
        status = classify_member(m)
        if status in stats:
            stats[status] += 1

        ts = m.get("trial_start")
        if ts:
            try:
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if ts_dt >= today_start:
                    stats["new_today"] += 1
            except Exception:
                pass

    # 直推人數
    direct_members = [m for m in members if m.get("referred_by") == agent["agent_id"]]
    stats["direct"] = len(direct_members)

    return stats

def get_members_paginated(agent, page=1, per_page=20, status_filter=None, search=None):
    """分頁取得會員列表"""
    descendants = get_all_descendants(agent)
    agent_ids = [agent["agent_id"]] + [a["agent_id"] for a in descendants]
    members = get_downline_members(agent, agent_ids)

    # Filter
    if status_filter:
        members = [m for m in members if classify_member(m) == status_filter]

    if search:
        search = search.upper()
        members = [m for m in members
                   if search in (m.get("referral_code") or "").upper()
                   or search in (m.get("display_name") or "").upper()
                   or search in (m.get("gw_account") or "").upper()
                   or search in (m.get("user_id") or "").upper()]

    # Sort: active first, then by expire_at desc
    def sort_key(m):
        s = classify_member(m)
        order = {"active": 0, "trial": 1, "new": 2, "expired": 3, "permanent": 4}
        return (order.get(s, 5), m.get("expire_at") or "")

    members.sort(key=sort_key)

    total = len(members)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_members = members[start:start + per_page]

    # Annotate with status + TW time
    TW = timezone(timedelta(hours=8))
    for m in page_members:
        m["_status"] = classify_member(m)
        if m.get("expire_at"):
            try:
                exp_dt = datetime.fromisoformat(m["expire_at"].replace("Z", "+00:00"))
                m["_expire_tw"] = exp_dt.astimezone(TW).strftime("%m/%d %H:%M")
            except Exception:
                m["_expire_tw"] = m["expire_at"][:16].replace("T", " ")
        else:
            m["_expire_tw"] = ""

    return {
        "members": page_members,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    }

def build_agent_tree(agent):
    """建構代理樹（遞迴）"""
    children = get_direct_children(agent)
    # 每個子代理的直推會員數
    node = {
        "agent_id": agent["agent_id"],
        "agent_code": agent.get("custom_ref_code") or agent["agent_code"],
        "display_name": agent.get("display_name") or agent.get("name") or agent["agent_code"],
        "depth": agent.get("depth", 1),
        "is_active": agent.get("is_active", True),
        "children": [],
    }

    # 直推會員數
    r = sb().table("members").select("user_id", count="exact") \
        .eq("referred_by", agent["agent_id"]) \
        .eq("tenant_id", agent["tenant_id"]) \
        .execute()
    node["direct_members"] = r.count if r.count is not None else len(r.data or [])

    for child in children:
        node["children"].append(build_agent_tree(child))

    return node
