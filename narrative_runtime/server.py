"""A small JSON-over-HTTP server for games that are not written in Python
(Unity, Godot, Unreal, web). Standard library only.

    python -m narrative_runtime serve --port 8765

    GET    /engine                    engine information
    POST   /games                     {"profile", "stats", "stats_mode", "seed", "mission", "persist", "start"}
    GET    /games/<id>                the current view
    POST   /games/<id>/choose         {"action": "<name>"} or {"action": <index>}
    GET    /games/<id>/transcript     the story so far
    POST   /games/<id>/end            end the session: progression saved, report sent to the UNP
    GET    /games/<id>/report         the ended session's report (409 while it goes on)
    GET    /progression/<profile>     the save game: goals achieved, playthroughs, session reports
    DELETE /games/<id>                forget the game (POST .../end first to count it as a session)
    POST   /evolve                    {"generations", "pop_size", "episodes", "max_steps", "seed", "resume"}
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import NarrativeEngine


def make_handler(engine: NarrativeEngine):
    lock = threading.Lock()  # one engine, one turn at a time

    class Handler(BaseHTTPRequestHandler):
        server_version = "AdaptiveNarrative/1"

        def log_message(self, *_args):  # keep the game's console quiet
            pass

        def _send(self, status, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}") if length else {}

        def _game(self, game_id):
            game = engine.games.get(game_id)
            if game is None:
                raise KeyError(game_id)
            return game

        def _route(self, method):
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            try:
                with lock:
                    if method == "GET" and parts == ["engine"]:
                        return self._send(200, engine.info())
                    if method == "GET" and len(parts) == 2 and parts[0] == "progression":
                        return self._send(200, engine.progression(parts[1]))
                    if method == "POST" and parts == ["games"]:
                        body = self._body()
                        game = engine.new_game(**{k: body[k] for k in
                                                  ("profile", "stats", "stats_mode", "seed", "mission", "persist", "start")
                                                  if k in body})
                        return self._send(201, game.view())
                    if len(parts) >= 2 and parts[0] == "games":
                        game = self._game(parts[1])
                        if method == "GET" and len(parts) == 2:
                            return self._send(200, game.view())
                        if method == "GET" and parts[2:] == ["transcript"]:
                            return self._send(200, {"game_id": game.id, "transcript": game.transcript()})
                        if method == "POST" and parts[2:] == ["choose"]:
                            return self._send(200, game.choose(self._body().get("action")))
                        if method == "POST" and parts[2:] == ["end"]:
                            return self._send(200, game.end())
                        if method == "GET" and parts[2:] == ["report"]:
                            report = game.report()
                            return self._send(200 if report else 409, report or {"error": "The session has not ended."})
                        if method == "DELETE" and len(parts) == 2:
                            engine.games.pop(game.id, None)
                            return self._send(200, {"deleted": game.id})
                    if method == "POST" and parts == ["evolve"]:
                        return self._send(200, engine.evolve(**self._body()))
                return self._send(404, {"error": f"No route {method} {self.path}"})
            except KeyError:
                return self._send(404, {"error": "Unknown game."})
            except (TypeError, ValueError) as exc:
                return self._send(400, {"error": str(exc)})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

        def do_OPTIONS(self):
            self._send(204, {})

    return Handler


def serve(engine=None, host="127.0.0.1", port=8765):
    engine = engine or NarrativeEngine()
    server = ThreadingHTTPServer((host, port), make_handler(engine))
    print(f"Adaptive Narrative engine '{engine.manifest.get('name')}' serving on http://{host}:{port}")
    server.serve_forever()
