"""Bound requests before parsing; never trust arbitrary forwarded client headers."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import tempfile
import threading
import time
from uuid import uuid4

import jwt
from starlette.responses import JSONResponse

from .auth import decode_access_token

log = logging.getLogger("geosyncai.security")
WEB_CSP = ("default-src 'self'; script-src 'self'; worker-src 'self' blob:; "
           "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob: https://tile.openstreetmap.org https://basemaps.cartocdn.com; "
           "connect-src 'self' https://tile.openstreetmap.org https://basemaps.cartocdn.com; "
           "object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")


class TrafficUnavailable(Exception):
    pass


class MemoryCounters:
    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()
        self.metrics = {}

    def consume(self, key, limit, window):
        now = time.monotonic()
        with self.lock:
            if key not in self.values and len(self.values) >= 10000:
                self.values = {k: v for k, v in self.values.items() if v[1] > now}
                if len(self.values) >= 10000:
                    raise TrafficUnavailable("Counter capacity exhausted")
            count, expiry = self.values.get(key, (0, now + window))
            if expiry <= now:
                count, expiry = 0, now + window
            self.values[key] = (count + 1, expiry)
            return count < limit, min(window, max(1, math.ceil(expiry - now)))

    def record(self, status):
        with self.lock:
            self.metrics[str(status)] = self.metrics.get(str(status), 0) + 1

    def snapshot(self):
        with self.lock:
            return dict(self.metrics)


class RedisCounters:
    SCRIPT = """
      local n = redis.call('INCR', KEYS[1])
      if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
      return {n <= tonumber(ARGV[1]) and 1 or 0, math.max(1, redis.call('TTL', KEYS[1]))}
    """

    def __init__(self, url):
        from redis import Redis
        self.client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, max_connections=20)

    def consume(self, key, limit, window):
        try:
            allowed, retry = self.client.eval(self.SCRIPT, 1, "geosyncai:security:rate:" + key, limit, window)
            return bool(allowed), int(retry)
        except Exception as exc:
            raise TrafficUnavailable("Shared traffic store unavailable") from exc

    def record(self, status):
        with self.client.pipeline() as pipe:
            pipe.hincrby("geosyncai:security:metrics", str(status), 1)
            pipe.expire("geosyncai:security:metrics", 86400)
            pipe.execute()

    def snapshot(self):
        return {k.decode(): int(v) for k, v in self.client.hgetall("geosyncai:security:metrics").items()}


class TrafficGuard:
    def __init__(self, settings):
        self.settings = settings
        url = settings.security_redis_url or settings.celery_broker_url
        self.store = RedisCounters(url) if url else MemoryCounters()
        self.lock = threading.Lock()
        self.active = self.heavy_active = 0

    def fingerprint(self, value):
        return hmac.new(self.settings.jwt_secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def consume(self, category, identity, limit, window=60):
        return self.store.consume(category + ":" + self.fingerprint(identity), limit, window)

    def admit(self, heavy):
        with self.lock:
            if self.active >= self.settings.max_concurrent_requests or (heavy and self.heavy_active >= self.settings.max_concurrent_heavy_requests):
                return False
            self.active += 1
            self.heavy_active += int(heavy)
            return True

    def release(self, heavy):
        with self.lock:
            self.active -= 1
            self.heavy_active -= int(heavy)

    def snapshot(self):
        try:
            statuses = self.store.snapshot()
        except Exception as exc:
            raise TrafficUnavailable("Traffic metrics unavailable") from exc
        return {"backend": "redis" if isinstance(self.store, RedisCounters) else "memory (single process)",
                "response_status_counts": statuses, "active_requests_this_process": self.active,
                "active_heavy_requests_this_process": self.heavy_active,
                "limits": {key: getattr(self.settings, key) for key in (
                    "rate_limit_enabled", "api_requests_per_minute", "login_requests_per_minute",
                    "account_login_requests_per_window", "account_login_window_seconds", "heavy_requests_per_minute",
                    "max_concurrent_requests", "max_concurrent_heavy_requests", "max_json_body_bytes", "max_upload_bytes")}}


def client_identity(scope, settings):
    peer = (scope.get("client") or ("unknown", 0))[0]
    try:
        address = ipaddress.ip_address(peer)
        trusted = any(address in ipaddress.ip_network(network) for network in settings.trusted_proxy_cidrs)
        forwarded = [value.decode() for key, value in scope["headers"] if key.lower() == b"x-geosyncai-client-ip"]
        if trusted and len(forwarded) == 1:
            return str(ipaddress.ip_address(forwarded[0]))
    except ValueError:
        pass
    return peer


class SecurityMiddleware:
    def __init__(self, app, guard):
        self.app, self.guard = app, guard

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        settings = self.guard.settings
        request_id = str(uuid4())
        scope.setdefault("state", {}).update(traffic_guard=self.guard, request_id=request_id)
        status, admitted, heavy = 500, False, False

        async def secured_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers += [(b"x-request-id", request_id.encode()), (b"x-content-type-options", b"nosniff"),
                            (b"x-frame-options", b"DENY"), (b"referrer-policy", b"strict-origin-when-cross-origin"),
                            (b"permissions-policy", b"geolocation=(self), camera=(self), microphone=()")]
                if scope["path"].startswith("/api/"):
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers += [(b"cache-control", b"no-store"), (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")]
                if scope.get("scheme") == "https":
                    headers.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": headers}
            await send(message)

        async def reject(code, detail, retry=None):
            headers = {"Retry-After": str(retry)} if retry else {}
            await JSONResponse({"detail": detail, "request_id": request_id}, code, headers=headers)(scope, receive, secured_send)

        try:
            path = scope["path"]
            headers = scope["headers"]
            if sum(len(k) + len(v) for k, v in headers) > 16384 or len(path) > 2048:
                return await reject(431, "Request headers or path too large")
            heavy = scope["method"] == "POST" and (path in {"/api/auth/token", "/api/auth/password", "/api/admin/users"} or any(part in path for part in (
                "/upload", "/jobs", "/validate", "/publish", "/geometry-drafts", "/geometry-changes",
                "/ground-control", "/train", "/reconciliation")))
            upload = path.endswith("/upload")
            auth = dict(headers).get(b"authorization", b"").decode(errors="replace")
            identity = "ip:" + client_identity(scope, settings)
            claims = None
            if auth.startswith("Bearer "):
                try:
                    claims = decode_access_token(auth[7:])
                except jwt.PyJWTError:
                    pass
            if claims and path != '/api/auth/token':
                identity = "user:" + claims["sub"]
            if settings.rate_limit_enabled:
                login = path == "/api/auth/token"
                limit = settings.login_requests_per_minute if login else settings.api_requests_per_minute
                allowed, retry = await asyncio.to_thread(self.guard.consume, "login-ip" if login else "api", identity, limit)
                if not allowed:
                    return await reject(429, "Too many requests; retry after the indicated delay", retry)
                if heavy:
                    allowed, retry = await asyncio.to_thread(self.guard.consume, "heavy", identity, settings.heavy_requests_per_minute)
                    if not allowed:
                        return await reject(429, "Processing request limit reached", retry)
            if upload and not claims:
                return await reject(401, "Valid authentication is required before uploading")
            admitted = self.guard.admit(heavy)
            if not admitted:
                return await reject(503, "Server is busy; retry shortly", 2)
            lengths = [v for k, v in headers if k.lower() == b"content-length"]
            if len(lengths) > 1 or (lengths and (not lengths[0].isdigit())):
                return await reject(400, "Invalid Content-Length")
            content_length = int(lengths[0]) if lengths else None
            if lengths and any(k.lower() == b"transfer-encoding" for k, _ in headers):
                return await reject(400, "Ambiguous request framing")
            if dict(headers).get(b"content-encoding", b"identity").lower() != b"identity":
                return await reject(415, "Compressed request bodies are not supported")
            limit = settings.max_upload_bytes + 65536 if upload else settings.max_json_body_bytes
            if content_length is not None and content_length > limit:
                return await reject(413, "Request body exceeds the configured limit")
            # Spool with a small memory threshold. Count real streamed bytes even
            # without Content-Length before FastAPI's JSON/multipart parsers run.
            with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body:
                size = 0
                async with asyncio.timeout(settings.request_body_timeout_seconds):
                    while True:
                        message = await asyncio.wait_for(receive(), settings.request_read_timeout_seconds)
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        size += len(chunk)
                        if size > limit:
                            return await reject(413, "Request body exceeds the configured limit")
                        await asyncio.to_thread(body.write, chunk)
                        if not message.get("more_body", False):
                            break
                if content_length is not None and content_length != size:
                    return await reject(400, "Request body length does not match Content-Length")
                body.seek(0)
                delivered = False
                async def replay():
                    nonlocal delivered
                    if delivered:
                        return await receive()
                    chunk = body.read(65536)
                    delivered = body.tell() == size
                    return {"type": "http.request", "body": chunk, "more_body": not delivered}
                await self.app(scope, replay, secured_send)
        except TimeoutError:
            await reject(408, "Request body upload timed out")
        except TrafficUnavailable:
            await reject(503, "Traffic protection is temporarily unavailable", 5)
        finally:
            if admitted:
                self.guard.release(heavy)
            try:
                await asyncio.to_thread(self.guard.store.record, status)
            except Exception:
                log.warning("Traffic metric store unavailable")
            if status in {401, 403, 408, 413, 429, 503}:
                log.info(json.dumps({"event": "request_rejected", "request_id": request_id, "status": status}))
