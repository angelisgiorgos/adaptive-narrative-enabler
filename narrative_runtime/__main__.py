"""Command line of an exported engine.

    python -m narrative_runtime info
    python -m narrative_runtime play  [--profile NAME] [--stats random|balanced|defined] [--seed N]
    python -m narrative_runtime serve [--host 127.0.0.1] [--port 8765]
    python -m narrative_runtime evolve [--generations 5] [--pop-size 12] [--episodes 3]
"""
import argparse
import json

from .engine import NarrativeEngine


def play(engine, args):
    game = engine.new_game(profile=args.profile, stats_mode=args.stats, seed=args.seed)
    view = game.view()
    if view["mission"]:
        print(f"Mission: {view['mission']} - {view['mission_description'] or ''}")
    while not view["ended"]:
        print(f"\n== {view['scene']} ==  health {view['health']}/{view['max_health']} · coins {view['coins']}")
        print(view["description"] or "")
        for number, name in enumerate(view["actions"], start=1):
            print(f"  [{number}] {name}")
        choice = input("> ").strip()
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
    parser.add_argument("--root", help="engine export folder (default: this package's export)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("info")
    play_cmd = commands.add_parser("play")
    play_cmd.add_argument("--profile")
    play_cmd.add_argument("--stats", choices=["random", "balanced", "defined"])
    play_cmd.add_argument("--seed", type=int)
    serve_cmd = commands.add_parser("serve")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8765)
    evolve_cmd = commands.add_parser("evolve")
    evolve_cmd.add_argument("--generations", type=int, default=5)
    evolve_cmd.add_argument("--pop-size", type=int, default=12)
    evolve_cmd.add_argument("--episodes", type=int, default=3)
    evolve_cmd.add_argument("--seed", type=int)
    args = parser.parse_args(argv)

    engine = NarrativeEngine(args.root)
    if args.command == "info":
        print(json.dumps(engine.info(), indent=2))
    elif args.command == "play":
        play(engine, args)
    elif args.command == "serve":
        from .server import serve
        serve(engine, args.host, args.port)
    elif args.command == "evolve":
        summary = engine.evolve(args.generations, args.pop_size, args.episodes, seed=args.seed,
                                progress=lambda g, total, s: print(f"Generation {g + 1}/{total}: benchmark {s.validation:.1f}"))
        print(f"New {summary['new_storyteller_score']:.1f} vs current {summary['current_storyteller_score']:.1f}: "
              + ("adopted the new storyteller." if summary["adopted"] else "kept the current storyteller."))


if __name__ == "__main__":
    main()
