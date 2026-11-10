"""Local server and administrator setup entry points."""
import argparse
import sqlite3
import warnings
from getpass import GetPassWarning, getpass

from werkzeug.security import generate_password_hash

from election_system import create_app, db
from election_system.auth import MIN_PASSWORD_LENGTH, is_valid_new_password


def _prompt_admin_password():
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = getpass("New password: ")
            confirmation = getpass("Confirm password: ")
    except GetPassWarning as error:
        raise ValueError("Password entry requires a terminal that can hide input.") from error

    if password != confirmation:
        raise ValueError("The passwords do not match.")
    if not is_valid_new_password(password):
        raise ValueError(f"Use at least {MIN_PASSWORD_LENGTH} characters, including an ASCII letter and digit.")
    return generate_password_hash(password)


def _configure_administrator():
    admins = {row["username"]: row["id"] for row in db.list_administrators()}
    if not admins:
        username = input("New administrator username: ").strip()
        if not username:
            raise ValueError("An administrator username is required.")
        db.create_first_administrator(username, _prompt_admin_password())
        print(f"Administrator account {username!r} created.")
        return

    print("Existing administrators:")
    for username in admins:
        print(f"  {username!r}")
    if input("Update administrator credentials? [y/N]: ").strip().lower() not in ("y", "yes"):
        return

    username = input("Administrator username to update: ").strip()
    if username not in admins:
        raise ValueError("Choose one of the listed administrators.")
    new_username = input("New administrator username [keep current]: ").strip() or username
    password_hash = _prompt_admin_password()
    db.update_administrator_credentials(admins[username], username, new_username, password_hash)
    print(f"Administrator account {new_username!r} updated.")

# -------------------------------------------------------------------------
# INITIALIZE DATABASE AND START THE APPLICATION
# -------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin", action="store_true", help="create or manage an administrator account")
    options = parser.parse_args(argv)

    try:
        local_app = create_app() if options.admin else create_app({"SESSION_COOKIE_SECURE": False})
        with local_app.app_context():
            db.init_db()
            if options.admin:
                _configure_administrator()
                return
    except (EOFError, KeyboardInterrupt):
        parser.exit(1, "Administrator setup cancelled.\n")
    except (ValueError, RuntimeError) as error:
        parser.exit(1, f"Error: {error}\n")
    except sqlite3.Error:
        parser.exit(1, "Error: Unable to access the election database.\n")

    local_app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
else:
    app = create_app()
