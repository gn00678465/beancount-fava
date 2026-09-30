from __future__ import annotations

import os
import sys

from cheroot.wsgi import Server
from fava.application import create_app

from beancount_agent_api.guard import ConfigError, guard, load_credentials

PORT = 5000


def main() -> None:
    try:
        credentials = load_credentials(os.environ)
    except ConfigError as error:
        sys.exit(f"beancount-fava-serve: {error}")

    filenames = [name for name in os.environ.get("BEANCOUNT_FILE", "").split(os.pathsep) if name]
    if not filenames:
        sys.exit("beancount-fava-serve: BEANCOUNT_FILE is not set")
    if relative := [name for name in filenames if not os.path.isabs(name)]:
        sys.exit(f"beancount-fava-serve: paths in BEANCOUNT_FILE must be absolute: {relative}")

    app = create_app(filenames)
    app.wsgi_app = guard(app.wsgi_app, credentials)

    host = os.environ.get("FAVA_HOST", "127.0.0.1")
    server = Server((host, PORT), app)
    print(f"Starting Fava on http://{host}:{PORT}", file=sys.stderr, flush=True)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
