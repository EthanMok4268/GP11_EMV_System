"""Development entry point for the election management system."""
from election_system import create_app
from election_system.db import init_db

app = create_app()

# -------------------------------------------------------------------------
# INITIALIZE DATABASE AND START THE APPLICATION
# -------------------------------------------------------------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="127.0.0.1", port=5000, debug=True)
