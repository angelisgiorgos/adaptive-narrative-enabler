"""Command line of an exported engine (an ANE AI): ``ane-ai <command>`` for a standalone build,
``python run.py <command>`` with Python.

    info                                   what is inside
    play  [--profile NAME] [--stats random|balanced|defined] [--seed N] [--start LOCATION]
    serve [--port 0] [--ready-file F] [--parent-pid PID] [--exit-on-stdin-eof] [--unp-url URL]
          [--allow-origin ORIGIN] [--allow-evolve] [--no-token]
          the UNP's server on 127.0.0.1 (narrative_runtime/server.py): the token comes from
          ANE_RUNTIME_TOKEN (or is made and given in the ready line)
    evolve [--generations 5] [--pop-size 12] [--episodes 3] [--resume]

Options of every command: --package DIR (the engine package; default: next to the program) and
--data-dir DIR (profiles, saves and the UNP's content; default: the package's saves/ folder, or
the user's application-data folder for a standalone build).
"""
import argparse
import json
import sys

from .engine import NarrativeEngine


def play(engine, args):
    game = engine.new_game(profile=args.profile, stats_mode=args.stats, seed=args.seed, start=args.start)
    view = game.view()
    if view["mission"]:
        print(f"Mission: {view['mission']} - {view['mission_description'] or ''}")
    while not view["ended"]:
        print(f"\n== {view['scene']} ==  health {view['health']}/{view['max_health']} · coins {view['coins']}")
        print(view["description"] or "")
        for number, name in enumerate(view["actions"], start=1):
            print(f"  [{number}] {name}")
        try:
            choice = input("> ").strip()
        except EOFError:  # the input ended (a pipe, Ctrl+D): stop like q
            print()
            break
        if choice.lower() in {"q", "quit", "exit"}:
            break
        if not choice.isdigit() or not 1 <= int(choice) <= len(view["actions"]):
            print("Choose a number from the list (q to quit).")
            continue
        view = game.choose(int(choice) - 1)
        print("\n" + view["turn"])
    if view["ended"]:
        print(f"\n{'Mission complete' if view['won'] else 'The end'}: {view['end_reason']}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m narrative_runtime", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--package", "--root", dest="root", help="engine package folder (default: next to the program)")
    parser.add_argument("--data-dir", help="where profiles, saves and the UNP's content are kept")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info")
    play_cmd = commands.add_parser("play")
    play_cmd.add_argument("--profile")
    play_cmd.add_argument("--stats", choices=["random", "balanced", "defined"])
    play_cmd.add_argument("--seed", type=int)
    play_cmd.add_argument("--start", help="starting location (default: drawn among the starting locations)")
    serve_cmd = commands.add_parser("serve")
    serve_cmd.add_argument("--host", default="127.0.0.1", help="127.0.0.1 (the only choice: local requests only)")
    serve_cmd.add_argument("--port", type=int, default=8765, help="0: any free port (given in the ready line)")
    serve_cmd.add_argument("--ready-file", help="write the ready line (JSON) to this file too")
    serve_cmd.add_argument("--parent-pid", type=int, help="stop when this process (the UNP) ends")
    serve_cmd.add_argument("--exit-on-stdin-eof", action="store_true", help="stop when stdin closes")
    serve_cmd.add_argument("--unp-url", help="the UNP's callback: session reports and events are POSTed there")
    serve_cmd.add_argument("--allow-origin", help="serve this one web origin (CORS); none by default")
    serve_cmd.add_argument("--allow-evolve", action="store_true", help="allow POST /evolve (offline training)")
    serve_cmd.add_argument("--no-token", action="store_true", help="no token (local development only)")
    evolve_cmd = commands.add_parser("evolve")
    evolve_cmd.add_argument("--generations", type=int, default=5)
    evolve_cmd.add_argument("--pop-size", type=int, default=12)
    evolve_cmd.add_argument("--episodes", type=int, default=3)
    evolve_cmd.add_argument("--seed", type=int)
    evolve_cmd.add_argument("--resume", action="store_true",
                            help="continue the saved training state exactly (data/training.json)")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .lifecycle import emit
        from .server import serve

        try:
            engine = NarrativeEngine(args.root, args.data_dir)
            code = serve(engine, args.host, args.port, ready_file=args.ready_file, parent_pid=args.parent_pid,
                         exit_on_stdin_eof=args.exit_on_stdin_eof, allow_origin=args.allow_origin,
                         allow_evolve=args.allow_evolve, unp_url=args.unp_url, no_token=args.no_token)
        except Exception as exc:  # noqa: BLE001 - the UNP reads why it did not start
            emit({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            sys.exit(1)
        sys.exit(code)
    engine = NarrativeEngine(args.root, args.data_dir)
    if args.command == "info":
        print(json.dumps(engine.info(), indent=2))
    elif args.command == "play":
        play(engine, args)
    elif args.command == "evolve":
        summary = engine.evolve(args.generations, args.pop_size, args.episodes, seed=args.seed, resume=args.resume,
                                progress=lambda g, total, s: print(f"Generation {g + 1}/{total}: benchmark {s.validation:.1f}"))
        print(f"New {summary['new_storyteller_score']:.1f} vs current {summary['current_storyteller_score']:.1f}: "
              + ("adopted the new storyteller." if summary["adopted"] else "kept the current storyteller."))


if __name__ == "__main__":
    main()
