"""Authentication, registration, and role checks."""
import sqlite3
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as database
from .db import get_db

bp = Blueprint("auth", __name__)


# -------------------------------------------------------------------------
# ACCESS CONTROL MIDDLEWARE (RBAC)
# -------------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return decorated


def admin_required(view):
    @wraps(view)
    def decorated(*args, **kwargs):
        if session.get("role") != "ADMIN":
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
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
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
        if not username or len(password) < 6:
            flash("Username required and password must be at least 6 characters.", "danger")
            return redirect(url_for("auth.register"))
        try:
            database.create_voter(username, generate_password_hash(password))
            get_db().commit()
            flash("Registration successful! Please log in.", "success")
            return redirect(url_for("auth.login"))
        except sqlite3.IntegrityError:
            flash("Username already registered. Duplicate registrations are prevented.", "danger")
    return render_template("register.html")


# -------------------------------------------------------------------------
# ROUTES: LOGOUT
# -------------------------------------------------------------------------
@bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
