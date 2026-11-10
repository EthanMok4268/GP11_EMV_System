"""Authentication, registration, and role checks."""
import hmac
import re
import sqlite3
from functools import wraps

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as database
from .db import get_db

bp = Blueprint("auth", __name__)
MIN_PASSWORD_LENGTH = 8
_NEW_PASSWORD_PATTERN = re.compile(
    r"(?=[\s\S]*[A-Za-z])"  # At least one English letter
    r"(?=[\s\S]*[0-9])"  # At least one digit
    rf"[\s\S]{{{MIN_PASSWORD_LENGTH},}}"
)


def is_valid_new_password(password):
    return _NEW_PASSWORD_PATTERN.fullmatch(password) is not None


def _uses_legacy_voter_credentials(username, password):
    return username == "voter1" and password == "voter123"


# -------------------------------------------------------------------------
# ACCESS CONTROL MIDDLEWARE (RBAC)
# -------------------------------------------------------------------------
def _session_fingerprint(user):
    message = f"login-session:{user['id']}:{user['password_hash']}".encode()
    return hmac.digest(current_app.secret_key.encode(), message, "sha256").hex()


@bp.before_app_request
def load_current_user():
    g.current_user = None
    if request.endpoint == "static" or "user_id" not in session:
        return

    user_id = session["user_id"]
    fingerprint = session.get("auth_fingerprint")
    if type(user_id) is not int or not isinstance(fingerprint, str) or not fingerprint.isascii():
        session.clear()
        return

    user = database.find_user_by_id(user_id)
    if user is None or not hmac.compare_digest(fingerprint, _session_fingerprint(user)):
        session.clear()
        return

    g.current_user = user
    if session.get("username") != user["username"] or session.get("role") != user["role"]:
        session.update(username=user["username"], role=user["role"])


def login_required(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        if g.current_user is None:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return decorated


def admin_required(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        if g.current_user is None or g.current_user["role"] != "ADMIN":
            flash("Access Denied: Administrator privileges required.", "danger")
            return redirect(url_for("elections.dashboard"))
        return view(*args, **kwargs)
    return decorated


# -------------------------------------------------------------------------
# ROUTES: AUTHENTICATION
# -------------------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = database.find_user(username)
        if (
            user
            and not (user["role"] == "VOTER" and _uses_legacy_voter_credentials(username, password))
            and check_password_hash(user["password_hash"], password)
        ):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["auth_fingerprint"] = _session_fingerprint(user)
            flash(f"Welcome back, {user['username']}!", "success")
            return redirect(url_for("elections.dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


# -------------------------------------------------------------------------
# ROUTES: VOTER REGISTRATION
# -------------------------------------------------------------------------
@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username:
            flash("Username is required.", "danger")
            return redirect(url_for("auth.register"))
        if not is_valid_new_password(password):
            flash(f"Use at least {MIN_PASSWORD_LENGTH} characters, including an English letter and a digit.", "danger")
            return redirect(url_for("auth.register"))
        if _uses_legacy_voter_credentials(username, password):
            flash("Choose a different password.", "danger")
            return redirect(url_for("auth.register"))
        try:
            database.create_voter(username, generate_password_hash(password))
            get_db().commit()
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("auth.login"))
        except sqlite3.IntegrityError:
            flash("Username already registered. Duplicate registrations are prevented.", "danger")
    return render_template(
        "register.html", min_password_length=MIN_PASSWORD_LENGTH, password_pattern=_NEW_PASSWORD_PATTERN.pattern,
    )


# -------------------------------------------------------------------------
# ROUTES: LOGOUT
# -------------------------------------------------------------------------
@bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
