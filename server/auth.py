import hashlib
import secrets
import time
from fastapi import Request, Response
from fastapi.responses import RedirectResponse

# ── Configuration ──
DASHBOARD_PASSWORD = "123456ad"  # Change this to your desired password
SESSION_COOKIE = "dtf_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# Active sessions: token -> expiry timestamp
_sessions: dict[str, float] = {}


def create_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_MAX_AGE
    return token


def is_valid_session(token: str) -> bool:
    if not token or token not in _sessions:
        return False
    if time.time() > _sessions[token]:
        _sessions.pop(token, None)
        return False
    return True


def check_password(password: str) -> bool:
    return password == DASHBOARD_PASSWORD


# Paths that do NOT require authentication (agent API endpoints)
PUBLIC_PATHS = {
    "/api/heartbeat",
    "/api/nest",
    "/api/unnest",
    "/login",
    "/favicon.ico",
    "/api/history",
    "/ws/dashboard",
}

PUBLIC_PREFIXES = (
    "/api/jobs/",  # /api/jobs/{id}/start, /api/jobs/{id}/complete
)


def is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DTF Monitor - Login</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'Inter', sans-serif;
    background: #0a0a0f;
    color: #e2e2e8;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
  }
  .login-box {
    background: #12121a;
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 16px;
    padding: 48px 40px;
    width: 380px;
    text-align: center;
  }
  .login-logo {
    width: 48px; height: 48px;
    background: linear-gradient(135deg, #a78bfa, #7c3aed);
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-weight: 900; font-size: 22px; color: #fff;
    margin: 0 auto 20px;
  }
  .login-title {
    font-size: 20px; font-weight: 700; margin-bottom: 6px;
  }
  .login-sub {
    font-size: 13px; color: #6b6b80; margin-bottom: 32px;
  }
  .login-input {
    width: 100%;
    padding: 14px 16px;
    background: #1a1a24;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px;
    color: #e2e2e8;
    font-size: 15px;
    font-family: 'JetBrains Mono', monospace;
    outline: none;
    transition: border-color 0.2s;
    text-align: center;
    letter-spacing: 2px;
  }
  .login-input:focus {
    border-color: #7c3aed;
  }
  .login-input::placeholder {
    color: #3a3a4a;
    letter-spacing: 0;
  }
  .login-btn {
    width: 100%;
    padding: 14px;
    background: linear-gradient(135deg, #7c3aed, #6d28d9);
    border: none;
    border-radius: 10px;
    color: #fff;
    font-size: 15px;
    font-weight: 600;
    font-family: 'Inter', sans-serif;
    cursor: pointer;
    margin-top: 16px;
    transition: opacity 0.2s;
  }
  .login-btn:hover { opacity: 0.9; }
  .login-error {
    color: #ef4444;
    font-size: 13px;
    margin-top: 12px;
    display: none;
  }
  .login-error.show { display: block; }
</style>
</head>
<body>
<div class="login-box">
  <div class="login-logo">D</div>
  <div class="login-title">DTF Floor Monitor</div>
  <div class="login-sub">Enter password to access dashboard</div>
  <form method="POST" action="/login" id="login-form">
    <input type="password" name="password" class="login-input" placeholder="Password" autofocus autocomplete="current-password">
    <button type="submit" class="login-btn">Login</button>
    <div class="login-error" id="login-error">Wrong password</div>
  </form>
</div>
<script>
  const params = new URLSearchParams(window.location.search);
  if (params.get('error') === '1') {
    document.getElementById('login-error').classList.add('show');
  }
</script>
</body>
</html>
"""
