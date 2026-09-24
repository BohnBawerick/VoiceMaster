"""Install the transcription-route patch in hermes gateway processes only.

The supervisor puts this directory on PYTHONPATH and sets
HERMES_INSTALL_TRANSCRIPTION_ROUTE=1 for `hermes gateway run` children
(default and discovered profiles). Other Python processes in the
container (webui, registry) do not get that env, so this is a no-op
there even if they inherit PYTHONPATH.
"""

import os
import sys

if os.environ.get("HERMES_INSTALL_TRANSCRIPTION_ROUTE") == "1":
    try:
        from hermes_transcription_route import install

        install()
    except Exception as exc:  # noqa: BLE001 - never take the gateway down
        print(
            "[transcription-route] install failed: %s: %s"
            % (type(exc).__name__, exc),
            file=sys.stderr,
        )
