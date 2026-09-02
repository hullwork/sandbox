#!/usr/bin/env python3
"""Process entrypoint for Control Plane API and Volume Agent roles."""

from __future__ import annotations

import hashlib
import threading
import time

from . import core as control_plane
from . import reaper as reaper_actions
from . import tracing


def reaper_loop() -> None:
    next_checkpoint_gc = 0.0
    while not control_plane._SHUTTING_DOWN.wait(15):
        try:
            removed = reaper_actions.reap_once()
            for kind, count in removed.items():
                if count:
                    control_plane.REAPER_ACTIONS.inc(count, kind=kind)
            if any(removed.values()):
                print(f"sandbox reaper removed {removed}", flush=True)
        except Exception as exc:
            print(f"sandbox reaper error: {exc}", flush=True)
        if time.monotonic() < next_checkpoint_gc:
            continue
        next_checkpoint_gc = time.monotonic() + control_plane.CHECKPOINT_GC_INTERVAL_SECONDS
        try:
            removed_checkpoints = reaper_actions.reap_expired_checkpoints()
            removed_tickets = reaper_actions.reap_expired_ticket_leases()
            if removed_checkpoints or removed_tickets:
                print(
                    "sandbox storage maintenance removed "
                    f"checkpoints={removed_checkpoints} "
                    f"ticket_leases={removed_tickets}",
                    flush=True,
                )
        except Exception as exc:
            print(f"sandbox storage maintenance error: {exc}", flush=True)


def run_volume_server() -> None:
    from . import volume

    print(
        f"volume agent listening on {control_plane.HOST}:{control_plane.PORT}, "
        f"volume={control_plane.WORKSPACE_VOLUME_ROOT}",
        flush=True,
    )
    server = control_plane.GracefulHTTPServer(
        (control_plane.HOST, control_plane.PORT), volume.VolumeHandler
    )
    control_plane.install_signal_handlers(server)
    server.serve_forever()
    control_plane.finish_shutdown(server)
    if not tracing.flush():
        print("[control_plane] trace flush timed out", flush=True)
    print("[control_plane] volume agent stopped", flush=True)


def run_api_server() -> None:
    from . import api

    if control_plane.STORE is not None:
        # A configured database owns authentication, quotas and ownership. Serving
        # after a failed migration creates a deceptively healthy Pod whose useful
        # routes all fail (or, worse, run against a partially upgraded schema).
        # Let StoreError terminate the process; Kubernetes will retry startup once
        # PostgreSQL is ready and an operator gets the real migration error.
        control_plane.STORE.ensure_schema()
        print(
            f"control plane store ready ({control_plane.STORE.backend})", flush=True
        )
    # Which sign-in methods are actually live, printed once at startup. The
    # break-glass token is the one people forget is still enabled, so it says so
    # in plain words rather than only as a fingerprint.
    if control_plane.LOCAL_LOGIN_ENABLED:
        fingerprint = hashlib.sha256(
            control_plane.SANDBOX_CONTROL_PLANE_TOKEN.encode("utf-8")
        ).hexdigest()[:8]
        local_login = f"enabled (break-glass, fingerprint={fingerprint})"
    else:
        local_login = "disabled"
    print(
        f"sandbox control plane listening on {control_plane.HOST}:{control_plane.PORT}, "
        f"oidc={'configured' if control_plane.OIDC_CONFIG else 'absent'}, "
        f"static control_plane token={local_login}",
        flush=True,
    )
    reaper_thread = threading.Thread(
        target=reaper_loop, name="reaper", daemon=True
    )
    reaper_thread.start()
    server = control_plane.GracefulHTTPServer((control_plane.HOST, control_plane.PORT), api.ApiHandler)
    control_plane.install_signal_handlers(server)
    server.serve_forever()
    control_plane.finish_shutdown(server, reaper_thread)
    if not tracing.flush():
        print("[control-plane] trace flush timed out", flush=True)
    print("[control-plane] stopped", flush=True)


def main() -> None:
    if not control_plane.SANDBOX_RUNTIME_CLASS:
        print(
            "[control_plane] WARNING: SANDBOX_RUNTIME_CLASS is empty; Runtime Pods "
            "run on the cluster default runtime WITHOUT gVisor kernel "
            "isolation. Untrusted code then shares the host kernel.",
            flush=True,
        )
    if control_plane.CONTROL_PLANE_ROLE == "volume":
        run_volume_server()
    else:
        run_api_server()


if __name__ == "__main__":
    main()
