"""OS-local operator CLI for the Wave 2 control plane.

No command accepts email, password, code, or token values via argv/environment.
Invite plaintext is written once to an interactive terminal and never logged.
"""

from __future__ import annotations

import argparse
import getpass
import hmac
import os
from pathlib import Path
import sys
import uuid

from lca import create_app
from lca.control_service import ControlPlaneRuntime, ServiceError, validate_uuid
from lca.control_security import utc_now


COMMANDS = (
    "bootstrap-owner-invite",
    "invite-create",
    "invite-revoke",
    "session-revoke",
    "tenant-suspend",
    "readiness-check",
)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        self.exit(2, "lca-admin: invalid arguments\n")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _operator_identity(runtime: ControlPlaneRuntime) -> str:
    if runtime.hasher is None or runtime.settings.secret_dir is None:
        raise RuntimeError("Operator security configuration is unavailable")
    allowlist_value = os.getenv("LCA_CP_OPERATOR_ALLOWLIST_FILE", "").strip()
    if not allowlist_value:
        raise RuntimeError("Operator allowlist is unavailable")
    allowlist_path = Path(allowlist_value).expanduser().resolve(strict=False)
    try:
        allowlist_path.relative_to(runtime.settings.secret_dir)
    except ValueError as exc:
        raise RuntimeError("Operator allowlist is unavailable") from exc
    if not allowlist_path.is_file():
        raise RuntimeError("Operator allowlist is unavailable")
    current = getpass.getuser()
    approved = [line.strip() for line in allowlist_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not any(hmac.compare_digest(current.casefold(), candidate.casefold()) for candidate in approved):
        raise PermissionError("Operator is not approved")
    return runtime.hasher.digest("audit-ref", current.casefold())


def _prompt_hidden(label: str) -> str:
    value = getpass.getpass(label).strip()
    if not value:
        raise ValueError("Required value missing")
    return value


def _print_invite(code: str) -> None:
    print("SENSITIVE: display this invite once and transfer it through an approved channel.")
    print(code)
    print("This CLI does not retain or display the plaintext again.")


def main(argv: list[str] | None = None) -> int:
    parser = SafeArgumentParser(prog="lca-admin")
    parser.add_argument("command", choices=COMMANDS)
    args = parser.parse_args(argv)

    if not _interactive():
        print("Refusing non-interactive operator session.", file=sys.stderr)
        return 2

    app = create_app({"RUNTIME_ROLE": "control-plane"})
    runtime: ControlPlaneRuntime = app.extensions["lca.control_runtime"]
    reasons = runtime.readiness_reasons()
    if args.command == "readiness-check":
        if reasons:
            print("NOT_READY " + ",".join(reasons))
            return 1
        print("READY (runtime boundary only; external launch remains unauthorized)")
        return 0
    if reasons:
        print("Control plane is not ready; no operator mutation performed.", file=sys.stderr)
        return 1

    request_id = str(uuid.uuid4())
    now = utc_now()
    try:
        actor_ref = _operator_identity(runtime)
        assert runtime.repository is not None
        if args.command == "bootstrap-owner-invite":
            email = _prompt_hidden("Owner email (hidden): ")
            _, code = runtime.bootstrap_invite(email, actor_ref=actor_ref, request_id=request_id, now=now)
            _print_invite(code)
            return 0
        if args.command == "invite-create":
            tenant_id = validate_uuid(input("Tenant UUID: ").strip())
            email = _prompt_hidden("Invite email (hidden): ")
            _, code = runtime.create_invite_for_tenant(
                email, tenant_id, actor_ref=actor_ref, request_id=request_id, now=now,
            )
            _print_invite(code)
            return 0
        if args.command == "invite-revoke":
            invite_id = validate_uuid(input("Invite UUID: ").strip())
            if not runtime.repository.revoke_invite(
                invite_id=invite_id, now=now, request_id=request_id, actor_ref=actor_ref,
            ):
                runtime.repository.append_audit(
                    now=now, event_type="invite.revoked", actor_type="operator", actor_ref=actor_ref,
                    outcome="denied", reason_code="NOT_FOUND", request_id=request_id,
                    target_type="invite", target_ref=invite_id,
                )
                print("Requested resource is unavailable.", file=sys.stderr)
                return 1
            print("Invite revoked.")
            return 0
        if args.command == "session-revoke":
            session_id = validate_uuid(input("Session UUID: ").strip())
            if not runtime.repository.operator_revoke_session(
                session_id=session_id, now=now, request_id=request_id, actor_ref=actor_ref,
            ):
                runtime.repository.append_audit(
                    now=now, event_type="session.revoked", actor_type="operator", actor_ref=actor_ref,
                    outcome="denied", reason_code="NOT_FOUND", request_id=request_id,
                    target_type="session", target_ref=session_id,
                )
                print("Requested resource is unavailable.", file=sys.stderr)
                return 1
            print("Session revoked.")
            return 0
        if args.command == "tenant-suspend":
            tenant_id = validate_uuid(input("Tenant UUID: ").strip())
            if not runtime.repository.suspend_tenant(
                tenant_id=tenant_id, now=now, request_id=request_id, actor_ref=actor_ref,
            ):
                runtime.repository.append_audit(
                    now=now, event_type="tenant.suspended", actor_type="operator", actor_ref=actor_ref,
                    outcome="denied", reason_code="NOT_FOUND", request_id=request_id,
                    target_type="tenant", target_ref=tenant_id,
                )
                print("Requested resource is unavailable.", file=sys.stderr)
                return 1
            print("Tenant suspended; active sessions/devices were revoked.")
            return 0
    except (OSError, PermissionError, RuntimeError, ValueError, ServiceError):
        print("Operator action denied; no sensitive detail was emitted.", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
