"""Local-only host/origin checks and stateless signed double-submit CSRF tokens."""
import hashlib
import hmac
import secrets
import re
from urllib.parse import urlsplit
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.secret = secrets.token_bytes(32)

    def valid(self, token):
        if not token or not re.fullmatch(r"[0-9a-f]{64}\.[0-9a-f]{64}", token):
            return False
        nonce, signature = token.split(".", 1)
        return hmac.compare_digest(signature, hmac.new(self.secret, nonce.encode(), hashlib.sha256).hexdigest())

    async def dispatch(self, request, call_next):
        if request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "Only localhost hosts are supported"}, status_code=400)
        token = request.cookies.get("local_csrf", "")
        if not self.valid(token):
            nonce = secrets.token_hex(32)
            token = nonce + "." + hmac.new(self.secret, nonce.encode(), hashlib.sha256).hexdigest()
        request.state.csrf_token = token
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                except ValueError:
                    return JSONResponse({"detail": "Invalid origin"}, status_code=403)
                if origin == "null" or parsed.scheme not in {"http", "https"} or parsed.netloc != request.url.netloc or parsed.scheme != request.url.scheme or parsed.path not in {"", "/"}:
                    return JSONResponse({"detail": "Local same-origin request required"}, status_code=403)
            elif request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
                return JSONResponse({"detail": "Local same-origin request required"}, status_code=403)
            content_type = request.headers.get("content-type", "").split(";")[0]
            if content_type in {"application/x-www-form-urlencoded", "multipart/form-data"} or not request.url.path.startswith("/api/"):
                await request.body()  # Cache for the downstream FastAPI form parser.
                form = await request.form()
                provided = str(form.get("csrf_token", ""))
                cookie = request.cookies.get("local_csrf", "")
                if not self.valid(cookie) or not hmac.compare_digest(provided.encode(), cookie.encode()):
                    return JSONResponse({"detail": "Valid CSRF token required"}, status_code=403)
            elif content_type and content_type != "application/json":
                return JSONResponse({"detail": "Local API requires application/json"}, status_code=415)
        response = await call_next(request)
        response.set_cookie("local_csrf", token, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response
