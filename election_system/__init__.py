"""Application factory for the election management system."""
import os

from flask import Flask


# -------------------------------------------------------------------------
# APPLICATION CONFIGURATION & BLUEPRINT REGISTRATION
# -------------------------------------------------------------------------
def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production"),
        DATABASE="election_system.db",
    )
    if test_config is not None:
        app.config.update(test_config)

    from . import db
    db.init_app(app)

    from . import admin, auth, elections, voting
    app.register_blueprint(auth.bp)
    app.register_blueprint(elections.bp)
    app.register_blueprint(voting.bp)
    app.register_blueprint(admin.bp)
    return app
