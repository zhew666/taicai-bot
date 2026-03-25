from flask import Blueprint
import os

def create_dashboard_blueprint():
    bp = Blueprint(
        "dashboard",
        __name__,
        template_folder="templates",
        static_folder="static_web",
        static_url_path="/dashboard/static",
    )

    from . import auth, views_page, views_api
    auth.init_app(bp)
    views_page.init_app(bp)
    views_api.init_app(bp)

    return bp
