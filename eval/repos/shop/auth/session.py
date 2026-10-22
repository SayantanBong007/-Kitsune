import hashlib
import hmac
import secrets

import db

SESSIONS = {}


def verify_password(stored_hash, password, salt):
    """Check a password against its salted PBKDF2 hash."""
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return hmac.compare_digest(digest, stored_hash)


def create_session(user_id):
    token = secrets.token_hex(16)
    SESSIONS[token] = user_id
    return token


def login(email, password):
    """Log a user in and return a session token."""
    row = db.query("SELECT id, hash, salt FROM users WHERE email = ?", email)[0]
    if not verify_password(row[1], password, row[2]):
        raise PermissionError("bad credentials")
    return create_session(row[0])
