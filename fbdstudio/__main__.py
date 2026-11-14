"""Start FBD Studio:  python -m fbdstudio [--workspace DIR] [--port 8765]"""

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .server import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="fbdstudio", description="Graphical function block programming for microcontrollers")
    ap.add_argument("--workspace", default=str(Path.home() / "FBDStudio"),
                    help="folder for projects, user blocks and builds (default: ~/FBDStudio)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="do not open a web browser")
    args = ap.parse_args()

    app = create_app(Path(args.workspace).expanduser())
    url = f"http://{args.host}:{args.port}/"
    print(f"FBD Studio running at {url}  (workspace: {Path(args.workspace).expanduser()})")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
