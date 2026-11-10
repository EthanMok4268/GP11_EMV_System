"""Application factory for the election management system."""
import os
import secrets

from flask import Flask


# -------------------------------------------------------------------------
# APPLICATION CONFIGURATION & BLUEPRINT REGISTRATION
# -------------------------------------------------------------------------
def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=True,
        DATABASE="election_system.db",
    )
    if test_config is not None:
        app.config.update(test_config)
        if app.testing and "SECRET_KEY" not in test_config:
            app.config["SECRET_KEY"] = secrets.token_urlsafe(32)

    secret_key = app.config["SECRET_KEY"]
    if (
        not isinstance(secret_key, str)
        or len(secret_key.strip()) < 32
        or secret_key.strip() == "dev-secret-key-change-in-production"
    ):
        raise RuntimeError("Set SECRET_KEY to a randomly generated value of at least 32 characters.")

    from . import db
    db.init_app(app)

    from . import admin, auth, elections, voting
    app.register_blueprint(auth.bp)
    app.register_blueprint(elections.bp)
    app.register_blueprint(voting.bp)
    app.register_blueprint(admin.bp)
    return app
