from flask import Blueprint
import os

BRAND_NAME = os.environ.get("BRAND_NAME", "百家之眼")

def create_dashboard_blueprint():
    bp = Blueprint(
        "dashboard",
        __name__,
        template_folder="templates",
        static_folder="static_web",
        static_url_path="/dashboard/static",
    )

    @bp.app_context_processor
    def inject_brand():
        return {"brand_name": BRAND_NAME}

    from . import auth, views_page, views_api
    auth.init_app(bp)
    views_page.init_app(bp)
    views_api.init_app(bp)

    return bp
