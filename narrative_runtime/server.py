"""The ANE AI's JSON-over-HTTP server: how a UNP (or any game engine: Unity, Godot, Unreal,
web) drives an exported engine on the same machine. Standard library only.

    ane-ai serve --port 0 --ready-file ready.json --parent-pid <UNP pid> [--unp-url URL]
    python run.py serve ...                     (the same, with Python)

It listens on 127.0.0.1 only. When it is ready it prints one JSON line on stdout (and writes
it to the ready file): {"event": "ready", "protocol", "url", "port", "pid", "engine",
"code_version", "token"?}. Every request needs ``Authorization: Bearer <token>``: the token
given in ANE_RUNTIME_TOKEN, else one made at start and given in the ready line.

    GET    /health                       up (or stopping), pid, engine, protocol, games
    GET    /engine                       engine information
    POST   /shutdown                     {"save": true, "end_sessions": false}: save every
                                         session, deliver everything, stop (exit code 0)
    GET    /world                        the world as parameters (ane.world/1)
    GET    /world/<kind>/<name>          one location, npc, item, event or mission
    POST   /world/patch                  world content from the UNP (see world.py); 422 + errors
    POST   /world/reset                  forget the UNP's content (the package's own world)
    GET    /progression/<profile>        the save game: goals achieved, playthroughs, reports
    GET    /games                        the active sessions
    POST   /games                        {"profile", "stats", "stats_mode", "seed", "mission",
                                         "persist", "start", "player": {...}} a new session, or
                                         {"savegame": {...}} to continue one (load game)
    GET    /games/<id>                   the current view
    GET    /games/<id>/state             the session's parameters (ane.session_state/1)
    PATCH  /games/<id>/player            set stats, inventory, coins, health, variables
    POST   /games/<id>/sync              what happened in the UNP: {"type": ...} or {"events": [...]}
    POST   /games/<id>/choose            {"action": "<name>"} or {"action": <index>}
    POST   /games/<id>/save              the save game (ane.savegame/1); the session goes on
    POST   /games/<id>/cancel            save game + status (suspended), sent to the UNP; released
    POST   /games/<id>/end               end it (counts as a session): progression + report
    GET    /games/<id>/report            the ended session's report (409 while it goes on)
    GET    /games/<id>/transcript        the story so far
    DELETE /games/<id>                   forget the game
    POST   /evolve                       offline training (only with --allow-evolve)
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import NarrativeEngine, PatchError

MAX_BODY = 8 * 1024 * 1024  # a save game is ~100 KB; a world patch rarely more
DRAIN_TIMEOUT = 5.0  # seconds serve() waits, once stopped, for answers still being written (UNPs kill after ~10 s)


class _HTTPError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


class RuntimeServer:
    """One engine served on 127.0.0.1 until it is shut down."""

    def __init__(self, engine, host="127.0.0.1", port=0, token=None, allow_origin=None, allow_evolve=False,
                 max_body=MAX_BODY, drain_timeout=DRAIN_TIMEOUT):
        self.engine = engine
        self.token = token
        self.allow_origin = allow_origin
        self.allow_evolve = allow_evolve
        self.max_body = max_body
        self.stopping = False
        self.stop_result = None
        self.stop_reason = None
        self._stop_lock = threading.Lock()
        self.drain_timeout = drain_timeout
        self._in_flight = 0
        self._idle = threading.Condition()
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self))
        self.httpd.daemon_threads = True
        self.host = host
        self.port = self.httpd.server_address[1]
        self.routes = _routes(self)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def ready_info(self, token_generated=False):
        from core_engine import version

        info = {"event": "ready", "protocol": version.PROTOCOL, "url": self.url, "port": self.port,
                "pid": os.getpid(), "engine": self.engine.manifest.get("name"),
                "code_version": version.code_version(), "data_dir": str(self.engine.data_dir)}
        if token_generated:
            info["token"] = self.token
        return info

    def serve(self):
        """Serve until shut down (the calling thread blocks). Before returning it waits (up to
        ``drain_timeout``) for the requests still being answered: the process exits right after,
        and the handler threads are daemons that would die mid-response."""
        try:
            self.httpd.serve_forever(poll_interval=0.2)
        finally:
            self.drain(self.drain_timeout)
            self.httpd.server_close()

    def request_started(self):
        with self._idle:
            self._in_flight += 1

    def request_finished(self):
        with self._idle:
            self._in_flight -= 1
            if not self._in_flight:
                self._idle.notify_all()

    def drain(self, timeout):
        """Wait until no request is being answered; False if ``timeout`` ran out first."""
        with self._idle:
            return self._idle.wait_for(lambda: not self._in_flight, timeout=timeout)

    def shutdown(self, save=True, end_sessions=False, reason="request", stop_serving=True):
        """Save (or end) every session, deliver everything, then stop serving. Safe to call
        more than once and from any thread except the one serving.

        ``stop_serving=False`` leaves the listener running so the caller can answer first (the
        ``POST /shutdown`` handler): once serving stops the process exits, and the daemon
        handler thread would die mid-response."""
        with self._stop_lock:
            if self.stopping:
                return self.stop_result or {"stopping": True}
            self.stopping, self.stop_reason = True, reason
        try:
            result = self.engine.shutdown(save=save, end_sessions=end_sessions)
            self.stop_result = {"stopping": True, "reason": reason, **result}
        finally:
            if stop_serving:
                self.stop_serving()
        return self.stop_result

    def stop_serving(self):
        """Make ``serve()`` return (from any thread; calling it again is harmless)."""
        threading.Thread(target=self.httpd.shutdown, name="ane-http-stop", daemon=True).start()


def _routes(server):
    engine = server.engine

    def game(game_id):
        found = engine.games.get(game_id)
        if found is None:
            raise _HTTPError(404, f"Unknown game `{game_id}`.")
        return found

    def new_game(body):
        if "savegame" in body:
            loaded, how, note = engine.load_savegame(body["savegame"], profile=body.get("profile"),
                                                     persist=body.get("persist", True), seed=body.get("seed"))
            return 201, {**loaded.view(), "restored": how, "note": note}
        options = {k: body[k] for k in ("profile", "stats", "stats_mode", "seed", "mission", "persist", "start", "player")
                   if k in body}
        return 201, engine.new_game(**options).view()

    def shutdown(body):
        # The handler stops serving once this answer is written (see Handler._route).
        return 200, server.shutdown(save=bool(body.get("save", True)), end_sessions=bool(body.get("end_sessions")),
                                    reason="POST /shutdown", stop_serving=False)

    def evolve(body):
        if not server.allow_evolve:
            raise _HTTPError(403, "Offline training is off for this server (start it with --allow-evolve).")
        return 200, engine.evolve(**body)

    def report(game_id):
        found = game(game_id).report()
        if found is None:
            raise _HTTPError(409, "The session has not ended; end it with POST .../end or cancel it.")
        return 200, found

    def forget(game_id):
        game(game_id)
        engine.games.pop(game_id, None)
        return 200, {"deleted": game_id}

    def health(_body=None):
        from core_engine import version

        return 200, {"status": "stopping" if server.stopping else "ok", "pid": os.getpid(),
                     "engine": engine.manifest.get("name"), "protocol": version.PROTOCOL,
                     "code_version": version.code_version(), "games": len(engine.games), "world_revision": engine.revision}

    G = r"(?P<id>[A-Za-z0-9_.-]+)"
    return [
        ("GET", r"/health", lambda m, b: health()),
        ("GET", r"/engine", lambda m, b: (200, engine.info())),
        ("POST", r"/shutdown", lambda m, b: shutdown(b)),
        ("GET", r"/world", lambda m, b: (200, engine.catalogue())),
        ("POST", r"/world/patch", lambda m, b: (200, engine.patch_world(b))),
        ("POST", r"/world/reset", lambda m, b: (200, engine.reset_patches())),
        ("GET", r"/world/(?P<kind>[a-z]+)/(?P<name>[^/]+)", lambda m, b: (200, engine.entity(m["kind"], m["name"]))),
        ("GET", r"/progression/(?P<profile>[^/]+)", lambda m, b: (200, engine.progression(m["profile"]))),
        ("GET", r"/games", lambda m, b: (200, {"games": [{"game_id": g.id, "profile": g.state.profile,
                                                          "ended": bool(g.state.ended)} for g in engine.games.values()]})),
        ("POST", r"/games", lambda m, b: new_game(b)),
        ("GET", rf"/games/{G}", lambda m, b: (200, game(m["id"]).view())),
        ("GET", rf"/games/{G}/state", lambda m, b: (200, game(m["id"]).parameters())),
        ("PATCH", rf"/games/{G}/player", lambda m, b: (200, game(m["id"]).update_player(b))),
        ("POST", rf"/games/{G}/sync", lambda m, b: (200, game(m["id"]).sync(b))),
        ("POST", rf"/games/{G}/choose", lambda m, b: (200, game(m["id"]).choose(b.get("action")))),
        ("POST", rf"/games/{G}/save", lambda m, b: (200, game(m["id"]).savegame())),
        ("POST", rf"/games/{G}/cancel", lambda m, b: (200, game(m["id"]).cancel())),
        ("POST", rf"/games/{G}/end", lambda m, b: (200, game(m["id"]).end())),
        ("GET", rf"/games/{G}/report", lambda m, b: report(m["id"])),
        ("GET", rf"/games/{G}/transcript", lambda m, b: (200, {"game_id": m["id"],
                                                               "transcript": game(m["id"]).transcript()})),
        ("DELETE", rf"/games/{G}", lambda m, b: forget(m["id"])),
        ("POST", r"/evolve", lambda m, b: evolve(b)),
    ]


def make_handler(server):
    from urllib.parse import unquote

    allowed_hosts = None

    class Handler(BaseHTTPRequestHandler):
        server_version = "ANE-AI/1"  # HTTP/1.0: one request per connection (a refused body never lingers)

        def log_message(self, *_args):  # stdout belongs to the lifecycle events
            pass

        def _send(self, status, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            origin = self.headers.get("Origin")
            if server.allow_origin and origin == server.allow_origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)

        def _check(self, method):
            nonlocal allowed_hosts
            if allowed_hosts is None:
                allowed_hosts = {f"127.0.0.1:{server.port}", f"localhost:{server.port}", f"[::1]:{server.port}"}
            if (self.headers.get("Host") or "").lower() not in allowed_hosts:
                raise _HTTPError(403, "Only local requests to 127.0.0.1 are served.")
            origin = self.headers.get("Origin")
            if origin and origin != server.allow_origin:
                raise _HTTPError(403, "Requests from web pages are not served (see --allow-origin).")
            if server.token is not None:
                sent = self.headers.get("Authorization") or ""
                if not sent.startswith("Bearer ") or not hmac.compare_digest(sent[7:].strip().encode(),
                                                                             server.token.encode()):
                    raise _HTTPError(401, "Missing or wrong token (Authorization: Bearer <token>).")
            if server.stopping and self.path.split("?")[0] != "/health":
                raise _HTTPError(503, "The engine is stopping.")

        def _body(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            if length > server.max_body:
                raise _HTTPError(413, f"The body is larger than {server.max_body} bytes.")
            if not length:
                return {}
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                raise _HTTPError(415, "Send JSON (Content-Type: application/json).")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError as exc:
                raise _HTTPError(400, f"The body is not JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise _HTTPError(400, "The body must be a JSON object.")
            return body

        def _route(self, method):
            server.request_started()  # serve() waits for this answer before the process exits
            try:
                self._dispatch(method)
            finally:
                # A POST /shutdown stops serving only after its answer is written.
                if method == "POST" and server.stopping and self.path.split("?")[0].rstrip("/") == "/shutdown":
                    server.stop_serving()
                server.request_finished()

        def _dispatch(self, method):
            from core_engine import resume, savegame, snapshot

            from .world import PatchFormatError

            try:
                self._check(method)
                body = self._body(method) if method in ("POST", "PATCH", "PUT") else {}
                path = unquote(self.path.split("?")[0]).rstrip("/") or "/"
                for verb, pattern, handler in server.routes:
                    match = re.fullmatch(pattern, path)
                    if match and verb == method:
                        with server.engine.lock:
                            status, payload = handler(match.groupdict(), body)
                        return self._send(status, payload)
                if any(re.fullmatch(pattern, path) for _, pattern, _ in server.routes):
                    raise _HTTPError(405, f"{method} is not allowed on {path}.")
                raise _HTTPError(404, f"No route {method} {path}")
            except _HTTPError as error:
                return self._send(error.status, {"error": error.message, **error.extra})
            except PatchError as exc:
                return self._send(422, {"error": "The world content does not fit.", "errors": exc.errors})
            except KeyError as exc:
                return self._send(404, {"error": str(exc.args[0] if exc.args else exc)})
            except (TypeError, ValueError, PatchFormatError, resume.ParameterError, savegame.SaveGameError,
                    snapshot.SnapshotError) as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - a broken request must not stop the engine
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

        def do_OPTIONS(self):
            origin = self.headers.get("Origin")
            if server.allow_origin and origin == server.allow_origin:
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(403, {"error": "Cross-origin requests are not served (see --allow-origin)."})

    return Handler


def serve(engine=None, host="127.0.0.1", port=8765, token=None, ready_file=None, parent_pid=None,
          exit_on_stdin_eof=False, allow_origin=None, allow_evolve=False, unp_url=None, no_token=False):
    """Run the server until it is shut down; returns the exit code (0: stopped cleanly)."""
    from . import lifecycle

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("The ANE AI only listens on the local machine (127.0.0.1).")
    if unp_url:
        os.environ["ANE_UNP_URL"] = unp_url
    engine = engine or NarrativeEngine()
    generated = False
    if not no_token:
        token = token or os.environ.get("ANE_RUNTIME_TOKEN") or None
        if token is None:
            token, generated = secrets.token_urlsafe(24), True
    else:
        token = None
    server = RuntimeServer(engine, "127.0.0.1", port, token=token, allow_origin=allow_origin,
                           allow_evolve=allow_evolve)

    def stop(reason):
        server.shutdown(save=True, end_sessions=False, reason=reason)

    lifecycle.install_signals(stop)
    if parent_pid:
        lifecycle.watch_parent(parent_pid, stop)
    if exit_on_stdin_eof:
        lifecycle.watch_stdin(stop)
    info = server.ready_info(token_generated=generated)
    if ready_file:
        lifecycle.write_ready_file(ready_file, info)
    lifecycle.emit(info)
    server.serve()
    lifecycle.emit({"event": "stopped", "reason": server.stop_reason, "pid": os.getpid(),
                    "saved": [s["game_id"] for s in (server.stop_result or {}).get("saved", [])]})
    return 0
