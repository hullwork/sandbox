#!/usr/bin/env python3
"""Domain orchestration and policy for the Sandbox Control Plane."""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import select
import signal
import socket
import subprocess
import tempfile
import threading
import time
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    IncompleteReadError,
    ReadTimeoutError,
    ResponseStreamingError,
)

import capability_ticket
from . import metrics as metrics_lib
from . import tracing, manifests, oidc, session
import workspace_contract
from .runtime_driver import (
    RuntimeDriver,
    RuntimeDriverError,
    RuntimeDriverErrorCode,
    RuntimeInstance,
    RuntimeSpec,
    RuntimeUsage,
)
from .drivers import GVisorRuntimeDriver
from .kube import KubeClient, KubeError
from .store import (
    GLOBAL_TENANT,
    UNTENANTED_RUNTIME,
    Store,
    StoreError,
    connection_hardening,
    require_driver,
)


HOST = os.getenv("SANDBOX_CONTROL_PLANE_HOST", "0.0.0.0")
PORT = int(os.getenv("SANDBOX_CONTROL_PLANE_PORT", "8080"))

# Missing required configuration is collected first and reported all at once at the end of import, before exiting.
#
# Constraint: do not change this back to `os.environ[...]`. A bare KeyError reports only the first missing name;
# operators fill that one in, roll again, and crash on the next one - a deployment mishap takes three or four
# rounds to converge, and what shows up on site is always CrashLoopBackOff, with the relevant line at the very
# bottom of the log. Reporting the full list at once + saying where each value comes from is the only problem solved here.
# Two roles from the same image:
#   control_plane - control plane. Manages Pods, signs tokens, and talks to object storage, but **cannot attach the Workspace volume**
#            (a PVC is a namespace-scoped resource; the volume lives in sandbox-workloads while the control_plane lives in
#            sandbox-system).
#   volume - volume agent. It mounts the whole volume and is the Control Plane's only hand on it: listing Workspaces, building
#            the directory structure, purging contents, and reading files when no Runtime is present.
# Split into two roles rather than two images because they share the same path sanitization and window caps - copy
# them out once and the two sides slowly drift apart. Must come before the first required_env: the role decides
# which configuration is required.
_RAW_CONTROL_PLANE_ROLE = os.getenv(
    "SANDBOX_CONTROL_PLANE_ROLE",
    "api",
).strip().lower()
CONTROL_PLANE_ROLE = _RAW_CONTROL_PLANE_ROLE

_CONFIG_ERRORS: list[str] = []


def required_env(name: str, *, hint: str) -> str:
    """Read a required setting; when it is missing, register the gap instead of raising on the spot.

    ``hint`` is mandatory and must be actionable - name who fills this item in (which Secret / which script),
    because most people who see this message are staring at a Pod in a restart loop and should not have to dig through a list again."""
    value = os.getenv(name)
    if value:
        return value
    _CONFIG_ERRORS.append(f"{name} is required; {hint}")
    return ""


# Browser identity comes from the deployment's own OpenID Connect provider.
# Configuration errors are collected rather than raised so that a deployment
# missing three settings learns about all three in one start.
OIDC_CONFIG, _oidc_errors = oidc.load_config(dict(os.environ))
_CONFIG_ERRORS.extend(_oidc_errors)
#: An issuer was named, whether or not the rest of the configuration is usable.
#: The default for local login must not flip to "on" because the OIDC block is
#: half configured - that would answer a broken provider with an open door.
OIDC_CONFIGURED = bool(os.getenv("SANDBOX_CONTROL_PLANE_OIDC_ISSUER", "").strip())


def _local_login_enabled() -> bool:
    """The static SANDBOX_CONTROL_PLANE_TOKEN admin path: break-glass, not the daily route.

    The rule is fixed:
        OIDC configured      -> off by default (explicitly switchable back on)
        OIDC not configured  -> on by default (otherwise nobody can sign in)
        both switched off    -> refuse to start, and say why

    🔴 Off means the credential is not accepted by the authentication path at
    all. Hiding the field in the Console and leaving the API route live is the
    classic way this control is lost: Gitea splits "hide the form" from "disable
    BASIC" into two settings for exactly that reason.
    """
    raw = os.getenv("SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        if not OIDC_CONFIGURED:
            _CONFIG_ERRORS.append(
                "SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED is off and SANDBOX_CONTROL_PLANE_OIDC_ISSUER is "
                "unset; no identity could sign in to this Control Plane. Configure the "
                "OIDC provider, or leave local login on"
            )
        return False
    if raw:
        _CONFIG_ERRORS.append(
            "SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED must be a boolean (true/false)"
        )
    return not OIDC_CONFIGURED


LOCAL_LOGIN_ENABLED = _local_login_enabled()
if (
    not LOCAL_LOGIN_ENABLED
    and OIDC_CONFIG is not None
    and not OIDC_CONFIG.admin_groups
):
    _CONFIG_ERRORS.append(
        "SANDBOX_CONTROL_PLANE_LOCAL_LOGIN_ENABLED is off, so "
        "SANDBOX_CONTROL_PLANE_OIDC_ADMIN_GROUPS must map at least one identity "
        "to the management plane; a tenant-only mapping cannot create the first "
        "tenant or administrator key"
    )
#: Whether Console cookies carry the Secure attribute and the __Host- prefix.
#: Decided from the redirect URL this deployment registered, never from a
#: request header - a value a client can set must not choose which cookie the
#: server looks for.
CONSOLE_COOKIES_SECURE = (
    OIDC_CONFIG is None or OIDC_CONFIG.redirect_url.startswith("https://")
)
#: 🔴 The shape of a pseudonymous subject: 32 lowercase hex characters, and
#: nothing else. Lowercase hex is simultaneously legal under every identifier
#: rule on either side of this boundary, which is why it was chosen - it needs
#: no agreement between the two deployments about a shared character class.
ACTING_SUBJECT_RE = re.compile(r"[0-9a-f]{32}")
# 🔴 Read only while the break-glass path is enabled. Leaving the value loaded
# and rejecting it later leaves one more branch that can be got wrong; an empty
# string cannot match any credential, whatever the branch does.
SANDBOX_CONTROL_PLANE_TOKEN = required_env(
    "SANDBOX_CONTROL_PLANE_TOKEN",
    hint="control-plane-token key in Secret sandbox-api-credentials",
) if LOCAL_LOGIN_ENABLED else ""
SIGNING_KEY = required_env(
    "SIGNING_KEY",
    hint="signing-key key in Secret sandbox-api-credentials",
).encode("utf-8")
# Derived key for workspace_id. No fallback to SIGNING_KEY: a deployment that silently
# derived from SIGNING_KEY would lose every Workspace ID the day that key is rotated.
# Deployments that ran with the old fallback must set this to the SIGNING_KEY value
# they derived with (scripts/bootstrap-local-secrets.sh does exactly that).
#
# 🔴 Why it is separate from SIGNING_KEY: SIGNING_KEY signs **short-lived** credentials (scoped tokens,
# object tickets, internal tokens) and should be replaced immediately if it leaks. But the same key used to derive
# workspace_id as well, and that is a **permanent identifier** - changing the key changes the IDs of all existing
# Workspaces: the same session would point at an empty directory again, and the old directory would sit unclaimed on the volume.
# Tying the two together meant SIGNING_KEY was effectively not rotatable.
#
# 🔴 Constraint: once this key is in production it is never rotated; the consequences are the same as above. Do not
# replace it alongside a SIGNING_KEY rotation - their life cycles are opposites.
WORKSPACE_ID_KEY = required_env(
    "WORKSPACE_ID_KEY",
    hint="workspace-id-key key in Secret sandbox-api-credentials",
).encode("utf-8")
# Console session and OIDC flow signing. A derived subkey rather than
# SIGNING_KEY itself, and never SANDBOX_CONTROL_PLANE_TOKEN - see control_plane/session.py.
SESSION_SECRET = session.derive_secret(SIGNING_KEY)
# Static credential between the Control Plane and the volume agent. Required for the volume role (it recognizes the Control Plane
# by it); optional for the Control Plane - not configured means there is no fallback read path. Deliberately not reusing
# SIGNING_KEY: that key can sign a scoped token for any Workspace and must not appear in
# sandbox-workloads.
if CONTROL_PLANE_ROLE == "volume":
    VOLUME_AGENT_TOKEN = required_env(
        "VOLUME_AGENT_TOKEN",
        hint="token key in Secret sandbox-volume-auth",
    )
else:
    VOLUME_AGENT_TOKEN = os.getenv("VOLUME_AGENT_TOKEN", "")
WORKLOAD_NAMESPACE = os.getenv("SANDBOX_NAMESPACE", "sandbox-workloads")
SYSTEM_NAMESPACE = os.getenv("SANDBOX_SYSTEM_NAMESPACE", "sandbox-system")
WORKSPACE_PVC = os.getenv("WORKSPACE_PVC", "sandbox-workspaces")
WORKSPACE_STORAGE_MODE = os.getenv(
    "SANDBOX_WORKSPACE_STORAGE_MODE", "shared"
).strip().lower()
if WORKSPACE_STORAGE_MODE not in {"per-workspace", "shared"}:
    raise SystemExit(
        "control_plane: SANDBOX_WORKSPACE_STORAGE_MODE must be per-workspace or shared"
    )
# Per-workspace PVCs are optional for storage providers that can expose the same
# workspace directories to the volume service. The portable deployment uses one
# RWX claim and mounts a workspace-specific subPath into each Runtime Pod.
WORKSPACE_QUOTA = os.getenv("SANDBOX_WORKSPACE_QUOTA", "1Gi")
# Mount point of the whole volume (not the subPath) inside the container. **Only the volume role mounts it** -
# the Control Plane cannot mount it itself: a PVC is a namespace-scoped resource, and the volume lives in sandbox-workloads while
# the Control Plane lives in sandbox-system. This is the whole reason the volume role exists: it is the Control Plane's
# only way to read a Workspace when no Runtime is around.
WORKSPACE_VOLUME_ROOT = os.getenv("SANDBOX_CONTROL_PLANE_WORKSPACE_ROOT", "/workspaces")
# Service DNS of the volume agent. Empty for the control_plane role = no volume agent deployed; a read request while the
# Runtime is absent then reports 409 truthfully instead of silently degrading to "file does not exist".
VOLUME_AGENT_URL = os.getenv("VOLUME_AGENT_URL", "")
RUNTIME_IMAGE = os.getenv(
    "SANDBOX_RUNTIME_IMAGE", "sandbox-runtime:0.5.0"
)
# Control-plane driver identity is separate from Kubernetes RuntimeClass. Only
# gVisor is implemented in this release; accepting another value would suggest
# a provider exists when the control plane still emits the gVisor Pod contract.
SANDBOX_RUNTIME_DRIVER = os.getenv(
    "SANDBOX_RUNTIME_DRIVER", "gvisor"
).strip().lower()
if SANDBOX_RUNTIME_DRIVER != "gvisor":
    raise SystemExit(
        "control plane: SANDBOX_RUNTIME_DRIVER must be gvisor in this release"
    )
# Configuration item: which runtimeClassName the Runtime Pod lands on.
#
# The local cluster installer configures a RuntimeClass with this name.
# Leave it empty to fall back to the cluster's default runtime. runtime_pod_manifest then **omits the key entirely**
# rather than writing an empty string: the two are equivalent to the kubelet, but when reading back an empty string
# it is hard to tell "not set" from "set to empty". sandbox_view.runtime_class and the e2e jsonpath assertions rely on that distinction.
# AI-LOCK: must be left empty when runsc is not installed on the node. Leave it at gvisor with the RuntimeClass object
# present, and the Pod schedules successfully and then gets stuck in RunContainerError, restarting repeatedly - much
# harder to troubleshoot than the apiserver refusing creation outright when the object does not exist.
SANDBOX_RUNTIME_CLASS = os.getenv("SANDBOX_RUNTIME_CLASS", "gvisor").strip()
# Configuration item: nodeSelector for the Runtime Pod (comma-separated k=v, e.g.
# "sandbox.convee.io/node-role=runtime"). Empty = no constraint; single-node cluster behavior is unchanged.
# Same "key omitted when not configured" semantics as SANDBOX_RUNTIME_CLASS: the read-back view and
# the e2e assertion rely on the key's presence to tell "not set" from "set to empty".
SANDBOX_RUNTIME_NODE_SELECTOR = {
    k: v
    for pair in os.getenv("SANDBOX_RUNTIME_NODE_SELECTOR", "").split(",")
    if pair.strip()
    for k, _, v in [pair.strip().partition("=")]
    if k and v
}


def _load_runtime_tolerations() -> tuple[dict[str, object], ...]:
    raw = os.getenv("SANDBOX_RUNTIME_TOLERATIONS", "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SANDBOX_RUNTIME_TOLERATIONS must be a JSON array") from exc
    if not isinstance(parsed, list) or len(parsed) > 16:
        raise ValueError("SANDBOX_RUNTIME_TOLERATIONS must contain at most 16 entries")
    allowed = {"key", "operator", "value", "effect", "tolerationSeconds"}
    result: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict) or not set(item) <= allowed:
            raise ValueError("invalid Runtime toleration fields")
        operator = item.get("operator", "Equal")
        effect = item.get("effect", "")
        if operator not in {"Equal", "Exists"} or effect not in {
            "", "NoSchedule", "PreferNoSchedule", "NoExecute",
        }:
            raise ValueError("invalid Runtime toleration operator or effect")
        if not isinstance(item.get("key", ""), str):
            raise ValueError("Runtime toleration key must be a string")
        if "value" in item and not isinstance(item["value"], str):
            raise ValueError("Runtime toleration value must be a string")
        if "tolerationSeconds" in item and (
            not isinstance(item["tolerationSeconds"], int)
            or item["tolerationSeconds"] < 0
            or effect != "NoExecute"
        ):
            raise ValueError("tolerationSeconds requires NoExecute and a non-negative integer")
        result.append(dict(item))
    return tuple(result)


SANDBOX_RUNTIME_TOLERATIONS = _load_runtime_tolerations()
TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Image references only get a character-class check: whether they exist is decided when the kubelet pulls them. The only
# guarantee here is that no whitespace/newline/shell metacharacters get written into the Pod spec.
_IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")


def _load_templates() -> dict[str, str]:
    """Configuration item: the template registry, template_id → runtime image.

    Responsibility: map the caller's optional template_id to an image; not responsible for building the image
         (that belongs to the image build path; this table only registers the build artifacts).
    Constraint: SANDBOX_TEMPLATES is a JSON object {"<id>": "<image>"}. Parsed at startup;
         a bad configuration refuses to start outright - consistent with the fail-fast style of the other settings in this file.
    AI-LOCK: the caller can never pass an image name directly. The Control Plane holds Pod-creation permission in the sandbox
         cluster; passing images through would allow any image to be pulled up in the cluster, exposing node disks and registry
         credentials. `/v1/sandboxes` only accepts registered ids, never an image field.
         'default' may not be overridden, otherwise "the default template was quietly replaced" cannot be detected from the request side."""
    templates = {"default": RUNTIME_IMAGE}
    raw = os.getenv("SANDBOX_TEMPLATES", "").strip()
    if not raw:
        return templates
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("SANDBOX_TEMPLATES must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("SANDBOX_TEMPLATES must be a JSON object")
    for key, value in parsed.items():
        if key == "default":
            raise ValueError(
                "SANDBOX_TEMPLATES must not redefine 'default'; "
                "set SANDBOX_RUNTIME_IMAGE instead"
            )
        if not isinstance(key, str) or not TEMPLATE_ID.fullmatch(key):
            raise ValueError(f"invalid template id: {key!r}")
        if not isinstance(value, str) or not _IMAGE_REF.fullmatch(value):
            raise ValueError(f"invalid image reference for template {key!r}")
        templates[key] = value
    return templates


SANDBOX_TEMPLATES = _load_templates()


def _load_image_registries() -> tuple[str, ...]:
    """Configuration item: prefix allowlist for template images, comma-separated.

    Responsibility: limit "which images the templates registered in the store may point to"; not responsible for whether the image exists, nor for
         the built-in templates (the other half of the same deployment configuration, see available_templates).
    Constraint: can only be given at deployment time through SANDBOX_IMAGE_REGISTRIES. **There is no API to change it** -
         an allowlist that can be changed through the API is no allowlist.
    AI-LOCK: fail closed when not configured (empty tuple = every template write gets 409, every existing row in the store
         stops taking effect). Never add a permissive default "for convenient testing": template = image = code that runs in the
         cluster; a permissive default hands over the right to create Pods. Empty items are discarded as part of this
         stance - `SANDBOX_IMAGE_REGISTRIES=","` cannot become a wildcard.
    Boundary: the prefix is a literal prefix and does not imply a directory boundary. `ghcr.io/convee` also lets
         `ghcr.io/convee-evil/x` through. For directory semantics, write the trailing `/` yourself.
         The `/` is not enforced: narrowing the allowlist down to an exact image reference (tag included) is a reasonable
         usage, and it would be impossible once a `/` is appended."""
    raw = os.getenv("SANDBOX_IMAGE_REGISTRIES", "")
    return tuple(prefix.strip() for prefix in raw.split(",") if prefix.strip())


SANDBOX_IMAGE_REGISTRIES = _load_image_registries()


def image_is_allowed(image: object) -> bool:
    """Whether this image reference may be registered as a template.

    Always False when the allowlist is not configured - any() over an empty tuple is False, so fail closed is the
    default behavior here rather than a branch someone has to remember to write."""
    if not isinstance(image, str) or not _IMAGE_REF.fullmatch(image):
        return False
    return any(image.startswith(prefix) for prefix in SANDBOX_IMAGE_REGISTRIES)


def available_templates(tenant_id: str | None) -> dict[str, str]:
    """The template_ids usable by this request → image: built-ins + the store rows visible to the tenant.

    Responsibility: merge two sources into one table; not responsible for authentication (the caller has already set tenant_id), nor
         for whether the image actually exists in the registry.
    Constraint: built-ins (SANDBOX_TEMPLATES) always take precedence. A same-named write is rejected at write time; the
         setdefault here is the second line of defense - a built-in added to the env later must also override an existing store row.
         Otherwise the "built-ins cannot be overridden" clause would work intermittently depending on deployment order.
    AI-LOCK: store rows must pass the current allowlist **one by one**. The allowlist is the deployment-time control over
         "what may run in the cluster"; tightening it must immediately invalidate the existing rows that no longer comply. Checking
         only at write time would make an allowlist change a no-op for the existing stock.
         Built-ins are **not** filtered by the allowlist: SANDBOX_TEMPLATES and the allowlist are both deployment configuration;
         filtering the former by the latter would only make every deployment that never set SANDBOX_IMAGE_REGISTRIES
         suddenly have no template at all after upgrading."""
    templates = dict(SANDBOX_TEMPLATES)
    if STORE is None:
        return templates
    for row in STORE.visible_templates(tenant_id):
        if image_is_allowed(row["image"]):
            templates.setdefault(str(row["template_id"]), str(row["image"]))
    return templates


def template_image(template_id: str, tenant_id: str | None = None) -> str:
    """Core function: template_id → image, the only exit.

    AI-LOCK: images come from here only. Any "just add an image parameter" change reopens the injection path that
         resolve_template closed - after which every layer in the call chain can slip in an image and the registry
         is merely decorative."""
    image = available_templates(tenant_id).get(template_id)
    if image is None:
        raise ValueError(f"unknown template: {template_id}")
    return image


def resolve_template(payload: dict, available: dict[str, str] | None = None) -> str:
    """Core function: resolve the request body to a registered template_id.

    Responsibility: the only gateway for image selection; not responsible for whether the image actually exists in the registry
         (the kubelet decides that when pulling).
    Constraint: reject an image/images field explicitly instead of ignoring it - ignoring it silently would let the caller
         believe it specified the image successfully, only to discover the default image later.
         By default ``available`` recognizes only the built-in registry: this function is pure validation; whether the store's
         templates count is decided by the caller (who knows which tenant this request represents)."""
    if "image" in payload or "images" in payload:
        raise ValueError(
            "image cannot be specified; register a template instead"
        )
    registry = SANDBOX_TEMPLATES if available is None else available
    template_id = payload.get("template_id", "default")
    if (
        not isinstance(template_id, str)
        or not TEMPLATE_ID.fullmatch(template_id)
    ):
        raise ValueError("invalid template_id")
    if template_id not in registry:
        raise ValueError(f"unknown template: {template_id}")
    return template_id


class TemplateWriteDisabled(RuntimeError):
    """Template writes are not available at all on this deployment (→ 409, not 400).

    Kept separate from "bad request" because the caller's next step differs: 400 means change the request and retry, 409 means this
    path is closed under the current deployment configuration, and changing the request is pointless."""


def validate_template_write(payload: dict) -> tuple[str, str, str]:
    """Validate one template write and return (tenant_id, template_id, image).

    Responsibility: verify **shape and access** only; does not check whether the tenant exists (that needs the store and is done by the caller),
         nor does it write the store.
    🔴 The validation order is itself the contract: **image allowlist first, then id validity**. The other way round, a
       request with an invalid id + a disallowed image would first get "invalid id"; the caller fixes the id and retries, only to learn the image is
       disallowed too - leaking the information over two round trips turns the allowlist into an interface that can be probed step by step."""
    if not SANDBOX_IMAGE_REGISTRIES:
        raise TemplateWriteDisabled(
            "template management is disabled: SANDBOX_IMAGE_REGISTRIES is not "
            "configured"
        )
    image = payload.get("image")
    if not image_is_allowed(image):
        # Do not return the allowlist contents: they are the cluster's internal topology; return the same id-only
        # reason as /v1/templates. An admin who wants to know what is configured reads the deployment configuration, not the API.
        raise ValueError(
            "image must be a valid reference under an allowed registry prefix"
        )
    template_id = payload.get("template_id")
    if (
        not isinstance(template_id, str)
        or not TEMPLATE_ID.fullmatch(template_id)
    ):
        raise ValueError("invalid template_id")
    if template_id in SANDBOX_TEMPLATES:
        # Built-ins (default included) cannot be overridden. Refuse on the spot instead of letting the row sit quietly in the store and be
        # ignored by available_templates - "written successfully but not in effect" is the hardest category to debug.
        raise TemplateWriteDisabled(
            f"{template_id} is a built-in template and cannot be overridden"
        )
    tenant_id = payload.get("tenant_id", GLOBAL_TENANT)
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("tenant_id must be a string")
    return tenant_id, template_id, str(image)


SANDBOX_TTL_SECONDS = int(os.getenv("SANDBOX_TTL_SECONDS", "1800"))
# LRU eviction threshold when the pool is full: a runtime idle beyond this value is released early to free a slot
# before a new session hits 429. Default 300s - shorter than the TTL (30min) but long enough to
# rule out collateral damage from "model thinking gaps": touch only advances when MCP/shell is active.
# In scenarios such as a message_user hang the pause lasts minutes, and 5 minutes is a safe idle threshold.
SANDBOX_IDLE_EVICT_SECONDS = int(
    os.getenv("SANDBOX_IDLE_EVICT_SECONDS", "300")
)
# Timeout for checking activity against the Runtime after the TTL has expired. What is checked is in-process state, so it should not be slow;
# a timeout is treated as "deletable" (see the AI-LOCK of probe_runtime_busy).
ACTIVITY_PROBE_TIMEOUT = float(os.getenv("SANDBOX_ACTIVITY_PROBE_TIMEOUT", "2"))
# Absolute upper limit, counted from creation time. Past it the Runtime is deleted without checking activity.
#
# This hard ceiling is independent of activity-based expiry and cannot be
# extended by touch requests.
RUNTIME_HARD_TTL_SECONDS = int(
    os.getenv("SANDBOX_RUNTIME_HARD_TTL_SECONDS", "43200")
)
WORKSPACE_IDLE_TTL_SECONDS = int(
    os.getenv("WORKSPACE_IDLE_TTL_SECONDS", "21600")
)


def workspace_ttl_advisory(idle_ttl: int, hard_ttl: int) -> str | None:
    """The startup warning for WORKSPACE_IDLE_TTL_SECONDS <= SANDBOX_RUNTIME_HARD_TTL_SECONDS, or None.

    A warning and not an assertion, on purpose: the defaults (6h idle vs 12h hard) have this shape in
    every existing deployment, and refusing to start would take all of them down. The shape is survivable
    now that Runtime life-cycle events and data-plane routes refresh the Workspace clock (touch_workspace);
    it was fatal when only workspace admission did. It is still worth a line, because a client that
    neither re-posts the lease nor touches the Workspace for idle_ttl seconds while its Runtime lives on
    is a client whose data goes the round the Runtime dies."""
    if idle_ttl > hard_ttl:
        return None
    return (
        f"WORKSPACE_IDLE_TTL_SECONDS={idle_ttl} is not above "
        f"SANDBOX_RUNTIME_HARD_TTL_SECONDS={hard_ttl}: a Workspace can reach its idle "
        "limit while its Runtime is still alive, and is then swept in the round "
        "the Runtime dies unless something refreshed it (file, object, checkpoint or "
        "Runtime activity through the Control Plane does)."
    )
CHECKPOINT_RETENTION_SECONDS = int(
    os.getenv("CHECKPOINT_RETENTION_SECONDS", "2592000")
)
CHECKPOINT_GC_INTERVAL_SECONDS = int(
    os.getenv("CHECKPOINT_GC_INTERVAL_SECONDS", "3600")
)
MAX_WORKSPACES = int(os.getenv("SANDBOX_MAX_WORKSPACES", "64"))
# The default matches the configuration in sandbox/k8s/ (8 × 768Mi = 6Gi namespace quota).
# Deploying to another topology requires adjusting workload-quota.yaml in core - see the red note there.
# The default must match the sandbox-tuning ConfigMap in kustomization.yaml, otherwise local runs and
# in-cluster runs have two sets of behavior (test_code_default_matches_the_manifest pins this).
# The premise of capacity 4 is that the file-service request was reduced to 128Mi (restore is now spooled):
# 4 × 281Mi (effective request) + 128Mi (volume) = 1252Mi ≤ quota 1300Mi (2GiB node).
MAX_RUNTIMES = int(os.getenv("SANDBOX_MAX_RUNTIMES", "4"))
# How long a pending record may stay before it counts as stale. Must be well above the provisioning ceiling (90s waiting for the Pod to be ready + health check),
# otherwise a sandbox still being provisioned is misjudged as stale and its quota is taken away.
PENDING_STALE_SECONDS = int(os.getenv("SANDBOX_PENDING_STALE_SECONDS", "600"))
# Upper limit on background creations. Provisioning in asynchronous mode no longer holds an HTTP connection, but it still holds a thread -
# without this gate a burst of requests can pile threads up in the Control Plane until it hits the memory limit, the same
# failure mode as mc. The default equals MAX_RUNTIMES: anything provisioning faster than that would have no quota anyway.
MAX_INFLIGHT_CREATES = int(
    os.getenv("SANDBOX_MAX_INFLIGHT_CREATES", str(MAX_RUNTIMES))
)
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "900"))
MAX_BODY_BYTES = 6 * 1024 * 1024
MAX_OBJECT_BYTES = 4 * 1024 * 1024
MAX_STREAM_OBJECT_BYTES = int(
    os.getenv("MAX_STREAM_OBJECT_BYTES", str(64 * 1024 * 1024))
)
# Ticket TTL upper limit. Every ticket carries a jti, consumed atomically through a Kubernetes
# Lease on first use; small files should still be given the shortest possible expires_in.
OBJECT_TICKET_TTL_SECONDS = int(
    os.getenv("OBJECT_TICKET_TTL_SECONDS", "900")
)
SANDBOX_ID = re.compile(r"^sb-[a-f0-9]{12}$")
WORKSPACE_ID = re.compile(r"^ws-[a-f0-9]{12}$")
OBJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Both segments accept common external identity forms, including email-like
# subjects. Control Plane owns this validation contract; it does not import a host's
# identity implementation.
OBJECT_OWNER = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$"
)
OBJECT_OWNER_ROOTS = {"uploads", "agents"}


def object_store_env(
    name: str,
    default: str | None = None,
) -> str:
    """Read one object-store setting and accumulate missing required values."""
    value = os.getenv(name)
    if value is not None:
        return value
    if default is None:
        _CONFIG_ERRORS.append(
            f"{name} is required; provide it through ConfigMap "
            "object-store-config and Secret object-store-credentials"
        )
        return ""
    return default


# Object storage only requires S3 compatibility: the endpoint can be a Service DNS name,
# http://<NodeIP>:<NodePort> across clusters, or a hosted service
# (https://s3.example.com or a provider-specific S3 endpoint). Buckets are not created by the Control Plane;
# the storage side initializes them, and only the names are known here.
OBJECT_STORE_ENDPOINT = object_store_env("OBJECT_STORE_ENDPOINT").rstrip("/")
OBJECT_STORE_ACCESS_KEY = object_store_env("OBJECT_STORE_ACCESS_KEY")
OBJECT_STORE_SECRET_KEY = object_store_env("OBJECT_STORE_SECRET_KEY")
OBJECT_STORE_UPLOAD_BUCKET = object_store_env(
    "OBJECT_STORE_UPLOAD_BUCKET",
    "user-uploads",
)
OBJECT_STORE_AGENT_BUCKET = object_store_env(
    "OBJECT_STORE_AGENT_BUCKET",
    "agent-data",
)
OBJECT_STORE_WORKSPACE_BUCKET = object_store_env(
    "OBJECT_STORE_WORKSPACE_BUCKET",
    "sandbox-workspaces",
)
# Storage probe path for /healthz. Empty by default, which skips the probe. A vendor-specific
# path such as /minio/health/ready is 404 on every other S3 implementation, so a default that
# named one would make the Control Plane's readinessProbe fail forever everywhere else. Set it
# only when the endpoint you deploy against has an anonymously reachable health endpoint.
OBJECT_STORE_HEALTH_PATH = os.getenv(
    "OBJECT_STORE_HEALTH_PATH", ""
)
OBJECT_STORE_SIGNATURE_VERSION = (
    os.getenv("OBJECT_STORE_SIGNATURE_VERSION", "S3v4").upper()
)
OBJECT_STORE_ADDRESSING_STYLE = os.getenv(
    "OBJECT_STORE_ADDRESSING_STYLE", "auto"
).lower()
if OBJECT_STORE_SIGNATURE_VERSION not in {"S3V2", "S3V4"}:
    raise ValueError(
        "OBJECT_STORE_SIGNATURE_VERSION must be S3v2 or S3v4"
    )
if OBJECT_STORE_ADDRESSING_STYLE not in {"auto", "virtual", "path"}:
    raise ValueError(
        "OBJECT_STORE_ADDRESSING_STYLE must be auto, virtual, or path"
    )
MC_CONFIG_LOCK = threading.Lock()
for _name, _value in (
    ("SANDBOX_TTL_SECONDS", SANDBOX_TTL_SECONDS),
    ("WORKSPACE_IDLE_TTL_SECONDS", WORKSPACE_IDLE_TTL_SECONDS),
    ("CHECKPOINT_RETENTION_SECONDS", CHECKPOINT_RETENTION_SECONDS),
    ("CHECKPOINT_GC_INTERVAL_SECONDS", CHECKPOINT_GC_INTERVAL_SECONDS),
    ("SANDBOX_MAX_WORKSPACES", MAX_WORKSPACES),
    ("SANDBOX_MAX_RUNTIMES", MAX_RUNTIMES),
):
    if _value <= 0:
        raise ValueError(f"{_name} must be greater than zero")
MAX_CONCURRENT_OBJECT_OPS = int(
    os.getenv("SANDBOX_MAX_CONCURRENT_OBJECT_OPS", "1")
)
if MAX_CONCURRENT_OBJECT_OPS <= 0:
    raise ValueError(
        "SANDBOX_MAX_CONCURRENT_OBJECT_OPS must be greater than zero"
    )

# Check after all required fields have been read, so that every missing one is reported in a single start.
# SystemExit instead of KeyError: what the container wants is an instruction to follow, not a stack trace.
if CONTROL_PLANE_ROLE == "volume":
    # 🔴 This role **MUST NOT** hold SIGNING_KEY. It runs in sandbox-workloads - the namespace of untrusted
    # workloads - and SIGNING_KEY can sign a scoped token for any Workspace.
    # It only needs the static VOLUME_AGENT_TOKEN to confirm "the caller is the Control Plane"; Workspace
    # ownership is verified by the Control Plane before forwarding, and directory isolation is guaranteed by local_safe_path.
    #
    # Same discipline as file-service: when provisioning a Pod the Control Plane writes the computed internal_token
    # straight into its env; it does not know the key either, so it cannot forge tokens for other Workspaces.
    _CONFIG_ERRORS = [
        error for error in _CONFIG_ERRORS if error.startswith("VOLUME_AGENT_TOKEN")
    ]
if _CONFIG_ERRORS:
    raise SystemExit(
        "control_plane: missing required configuration, "
        f"{len(_CONFIG_ERRORS)} item(s):\n  - "
        + "\n  - ".join(_CONFIG_ERRORS)
    )
# An object operation holds its whole body in memory, so several at once inside
# the 1GiB Control Plane cgroup is what OOMKills it when a few Agent runs persist
# messages or checkpoints together. Keep one in flight; request threads still
# accept concurrent work and queue below. This used to bound the RSS of `mc`
# child processes, one per operation; the reason survives the subprocess.
_OBJECT_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_OBJECT_OPS)
# 🔴 The semaphore above bounds the work in flight; it cannot bound the queued threads themselves.
# ThreadingHTTPServer spawns one thread per request with no connection cap, and the streaming
# timeout is 180s - concurrent object operations block every thread on the semaphore, the thread
# count grows without bound, and the Control Plane gets OOMKilled by its 1GiB cgroup. The reaper is
# a daemon thread in the same process and dies with it, so sandboxes are no longer reclaimed.
# Guarding the operations without guarding the queue is no guard at all.
# This gate turns "cannot wait" into an explicit fail-fast: 503 immediately when full so the caller retries, instead of
# piling up in memory. The depth is set by the resident overhead of a request thread and is unrelated to operation concurrency.
MAX_OBJECT_QUEUE = int(os.getenv("SANDBOX_MAX_OBJECT_QUEUE", "32"))
if MAX_OBJECT_QUEUE <= 0:
    raise ValueError("SANDBOX_MAX_OBJECT_QUEUE must be greater than zero")
_OBJECT_QUEUE_SLOTS = threading.BoundedSemaphore(MAX_OBJECT_QUEUE)


# --- Metrics ----------------------------------------------------------------
# 🔴 Deliberately no tenant label. Two reasons: the label cardinality would grow without bound with the number of tenants, and /metrics is
# unauthenticated (same level as /healthz, protected by NetworkPolicy), so carrying tenant names would hang the tenant
# list outside. To view usage by tenant, use the authenticated /v1/admin interfaces.
METRICS = metrics_lib.Registry()
RUNTIME_CREATE_SECONDS = METRICS.register(
    metrics_lib.Histogram(
        "sandbox_runtime_create_seconds",
        "Runtime from admission to healthy.",
        # Buckets follow the actual magnitude: the ceiling for waiting on Pod readiness is 90s, so the tail must tell 60 from 90,
        # rather than copying the default buckets that top out at 10 seconds.
        (1, 2, 5, 10, 20, 30, 60, 90),
    )
)
HTTP_REQUESTS = METRICS.register(
    metrics_lib.Counter(
        "sandbox_http_requests_total",
        "Control-plane requests by normalised route, method and status class.",
    )
)
HTTP_REQUEST_SECONDS = METRICS.register(
    metrics_lib.Histogram(
        "sandbox_http_request_seconds",
        "Control-plane request latency by normalised route and method.",
        (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
    )
)
RUNTIME_CREATE_PHASE_SECONDS = METRICS.register(
    metrics_lib.Histogram(
        "sandbox_runtime_create_phase_seconds",
        "Runtime creation latency by bounded provisioning phase.",
        (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 90),
    )
)
OBJECT_STORE_OPERATIONS = METRICS.register(
    metrics_lib.Counter(
        "sandbox_object_store_operations_total",
        "Object-store operations by bounded operation and outcome.",
    )
)
TRACE_EXPORT_DROPS = METRICS.register(
    metrics_lib.Counter(
        "sandbox_trace_export_drops_total",
        "Spans dropped before export by bounded reason.",
    )
)
for _reason in ("queue_full", "export_error"):
    TRACE_EXPORT_DROPS.ensure(reason=_reason)
tracing.set_drop_observer(lambda reason: TRACE_EXPORT_DROPS.inc(reason=reason))


@contextlib.contextmanager
def runtime_create_phase(phase: str):
    """Measure and trace one closed-set provisioning phase."""
    started = time.monotonic()
    with tracing.start_span(
        f"runtime.create.{phase}", attributes={"sandbox.runtime.phase": phase}
    ):
        try:
            yield
        finally:
            RUNTIME_CREATE_PHASE_SECONDS.observe(
                time.monotonic() - started, phase=phase
            )
RUNTIME_CREATE_FAILURES = METRICS.register(
    metrics_lib.Counter(
        "sandbox_runtime_create_failures_total",
        "Failed Runtime creations by reason.",
    )
)
QUOTA_REJECTIONS = METRICS.register(
    metrics_lib.Counter(
        "sandbox_quota_rejections_total",
        "Admissions refused by a quota gate.",
    )
)
REAPER_ACTIONS = METRICS.register(
    metrics_lib.Counter(
        "sandbox_reaper_actions_total",
        "Objects acted on by the reaper, by kind.",
    )
)
CREDENTIAL_USES = METRICS.register(
    metrics_lib.Counter(
        "sandbox_credential_uses_total",
        "Authenticated requests by credential kind.",
    )
)
AUDIT_FAILURES = METRICS.register(
    metrics_lib.Counter(
        "sandbox_audit_write_failures_total",
        "Audit rows that could not be written.",
    )
)
STORE_ERRORS = METRICS.register(
    metrics_lib.Counter(
        "sandbox_store_errors_total",
        "Control plane store failures surfaced to callers.",
    )
)
# Object tickets are the whole browser-facing upload/download path: the client
# gets a signed one-time ticket and spends it here. Every failure mode ends as
# an identical 401 to the caller, so a tenant whose uploads all fail on expired
# or replayed tickets was previously invisible - the Control Plane has no HTTP response
# counter either. `reason` separates the three that need different answers:
# a bad signature means the key rotated under someone, `claims_rejected` covers
# expiry and scope, and `replayed` means the ticket was already spent (a retry
# storm, or a client that does not know it succeeded).
OBJECT_TICKET_FAILURES = METRICS.register(
    metrics_lib.Counter(
        "sandbox_object_ticket_failures_total",
        "Object tickets refused, by reason.",
    )
)
for _reason in ("bad_signature", "claims_rejected", "malformed", "replayed"):
    OBJECT_TICKET_FAILURES.ensure(reason=_reason)
_OBJECT_INFLIGHT = 0
_OBJECT_INFLIGHT_LOCK = threading.Lock()
_CREATE_SLOTS = threading.BoundedSemaphore(MAX_INFLIGHT_CREATES)
_CREATE_INFLIGHT = 0
# 🔴 A Condition, not a Lock: the shutdown orchestration has to wait for these background provisionings to finish, and they run in create-*
# threads, not request threads - the HTTP-side in-flight count cannot see them at all (the 202 was returned long ago).
_CREATE_INFLIGHT_LOCK = threading.Condition()


def _create_inflight() -> float:
    with _CREATE_INFLIGHT_LOCK:
        return float(_CREATE_INFLIGHT)


def await_pending_creations(timeout: float) -> int:
    """Wait for background provisionings (create-* threads) to finish; return how many are still running at timeout (0 = drained).

    The asynchronous path has already returned the sandbox_id to the client, which is polling for it. Cutting the thread off has
    the same consequences as on the synchronous path: a pending row in the store holding a quota slot, and a running Pod in the cluster.
    The client then has to wait for stale-pending cleanup (10 minutes by default) before it can see a terminal state."""
    deadline = time.monotonic() + timeout
    with _CREATE_INFLIGHT_LOCK:
        while _CREATE_INFLIGHT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _CREATE_INFLIGHT
            _CREATE_INFLIGHT_LOCK.wait(remaining)
    return 0


def _object_queue_depth() -> float:
    with _OBJECT_INFLIGHT_LOCK:
        return float(_OBJECT_INFLIGHT)


METRICS.register(
    metrics_lib.Gauge(
        "sandbox_object_store_queue_depth",
        "Requests currently holding or waiting for an object store slot.",
        _object_queue_depth,
    )
)
# 🔴 Exported so the saturation alert can be a ratio instead of a literal.
# The queue ceiling is configurable, and a threshold hard-coded to today's
# default does not fail loudly when the variable is raised - it just stops
# firing. A saturation gauge without its limit is a bare number nobody can act
# on; with it, "how close are we to refusing object writes" is one expression.
METRICS.register(
    metrics_lib.Gauge(
        "sandbox_object_store_queue_limit",
        "Configured ceiling on queued object store requests.",
        lambda: float(MAX_OBJECT_QUEUE),
    )
)
METRICS.register(
    metrics_lib.Gauge(
        "sandbox_creates_inflight",
        "Runtime creations running in the background.",
        _create_inflight,
    )
)
# /metrics is unauthenticated (same level as /livez, /readyz, /healthz; NetworkPolicy decides who can scrape
# it), and the two gauges below hit the store. Store._cursor() opens a new connection every time while holding the
# process-level Store lock, and PostgreSQL's connect_timeout is 5s ⇒ once the store slows down, one acquisition can
# hold the lock for seconds, and every authenticated request makes at least 3 store calls. So an endpoint anyone can hit at high frequency
# can pin the entire authentication path - that is not an "inaccurate metric", it is an availability problem.
#
# Two gates, and the order cannot be reversed:
# ① Check the cache first. The TTL caps the store frequency at "once per TTL" and decouples it from the number of scrapers (the Prometheus
#   primary and replica, alerting rule evaluation, and manual curl all count as scrapers, and the endpoint is unauthenticated ⇒ the ceiling is not
#   ours to decide). This gate handles "high frequency".
#   🔴 The TTL must be **less** than the scrape interval. At or above it, two consecutive scrapes read the same
#   value and artificial steps appear on the curve - rate()/delta() are distorted at those steps, and those two
#   functions are exactly the input to the capacity alerts. 10s it is: the common scrape intervals of 15s/30s/60s all work.
#   Worst-case staleness = TTL + scrape interval, entirely sufficient for a "number of live sandboxes".
# ② Count only when the cache has expired, and **do not count if the Store lock cannot be grabbed**. The TTL cannot stop the one
#   fetch after expiry, and that one still stacks up when it lands on a queued request. This gate handles "stacking".
#   ⚠️ Boundary: it blocks "the store is busy", but not "the store is idle yet slow" - the lock is then still held for up to
#   connect_timeout. That residue is acceptable: when the store is slow every authenticated request already pays
#   the same price, and /metrics now pays at most once per TTL and no longer scales linearly with scrape frequency.
#
# 🔴 "Cannot count" must not be emitted as a value, and never falls back to 0. So the yielding path **raises** rather than
# returning 0.0 - Gauge.render emits an empty line after catching the exception, which is exactly what it was prepared for (metrics.py
# has dedicated instructions and test cases). Reporting "data source busy" as "system very idle" would point the capacity dashboard and alerts to
# the exact opposite conclusion.
STORE_GAUGE_TTL_SECONDS = float(
    os.getenv("SANDBOX_STORE_GAUGE_TTL_SECONDS", "10")
)
_STORE_GAUGE_CACHE: dict[str, tuple[float, float]] = {}
_STORE_GAUGE_CACHE_LOCK = threading.Lock()


class StoreGaugeUnavailable(RuntimeError):
    """This scrape cannot be counted. Let Gauge.render drop the whole metric instead of reporting 0."""


def store_gauge(name: str, count: Callable[[], int]) -> float:
    """Store count with TTL caching and "yield when the store is busy".

    Boundary: only **successful** values are cached. A failure does not write the cache - if it did, the recovery would not be seen until another
         TTL had passed, and "the store is no longer busy" is exactly the moment that should be seen immediately."""
    if STORE is None:
        # No store = single-tenant mode, where a store-backed count is inherently meaningless. 0 here is the true value, not a fallback.
        return 0.0
    now = time.monotonic()
    with _STORE_GAUGE_CACHE_LOCK:
        cached = _STORE_GAUGE_CACHE.get(name)
        if cached is not None and now - cached[0] < STORE_GAUGE_TTL_SECONDS:
            return cached[1]
    with STORE.try_lock() as acquired:
        if not acquired:
            raise StoreGaugeUnavailable(f"{name}: control plane store is busy")
        # 🔴 The count **must** happen inside the lock. Trying the lock, releasing it, and then counting would leave a window
        # in the middle where the gate is open, which means there is no gate. RLock is re-entrant, and _cursor() does not lock itself again.
        value = float(count())
    with _STORE_GAUGE_CACHE_LOCK:
        _STORE_GAUGE_CACHE[name] = (time.monotonic(), value)
    return value


METRICS.register(
    metrics_lib.Gauge(
        "sandbox_runtimes_live",
        "Runtimes not yet in a terminal state, across all tenants.",
        lambda: store_gauge(
            "sandbox_runtimes_live",
            lambda: STORE.count_all_live_runtimes(),
        ),
    )
)
METRICS.register(
    metrics_lib.Gauge(
        "sandbox_workspaces_registered",
        "Workspaces with a live ownership row, across all tenants.",
        lambda: store_gauge(
            "sandbox_workspaces_registered",
            lambda: STORE.count_all_workspaces(),
        ),
    )
)


# Same reason as OBJECT_TICKET_FAILURES above: without this, "no runtime
# creation has ever failed" and "this build does not emit that metric" are the
# same absent series on the scraper side, and the alerts in
# observability/alerts/ cannot tell a healthy Control Plane from an unwired one. The
# values are the closed sets produced by create_failure_reason and the three
# quota gates.
for _reason in (
    "quota", "released_while_starting", "namespace_quota", "forbidden",
    "kube_error", "not_ready_in_time", "store_error", "other",
):
    RUNTIME_CREATE_FAILURES.ensure(reason=_reason)
for _gate in ("global", "tenant", "tenant_workspace"):
    QUOTA_REJECTIONS.ensure(gate=_gate)
AUDIT_FAILURES.ensure()
STORE_ERRORS.ensure()


def create_failure_reason(exc: BaseException) -> str:
    """Group failures into a bounded set of categories. The label cardinality must stay closed; exception text may not be stuffed in."""
    if isinstance(exc, KubeError):
        if exc.status == HTTPStatus.TOO_MANY_REQUESTS:
            return "quota"
        if exc.status == HTTPStatus.CONFLICT:
            return "released_while_starting"
        if exc.status == HTTPStatus.FORBIDDEN:
            # 🔴 A namespace ResourceQuota rejects with 403, not 429 - recognizing only 429
            # would mean "the Control Plane's admission gate is wider than K8s" never shows up in the metric
            # and gets mixed into the kube_error pile. The two must be separated: a quota mismatch is a misconfiguration, an RBAC
            # error is a broken deployment, and the handling is completely different. Matching on the message instead of stuffing in the text keeps the label cardinality closed.
            if "exceeded quota" in str(exc):
                return "namespace_quota"
            return "forbidden"
        return "kube_error"
    if isinstance(exc, TimeoutError):
        return "not_ready_in_time"
    if isinstance(exc, StoreError):
        return "store_error"
    return "other"


class ObjectStoreBusy(RuntimeError):
    """The object storage path is temporarily unavailable. Retryable - not the same thing as "operation denied"."""


class ObjectStoreUnavailable(ObjectStoreBusy):
    """The endpoint is out of reach: the request never got an answer.

    Why it hangs under ObjectStoreBusy instead of being a separate type: the caller has to do exactly the same thing -
    wait a while and retry - and `except ObjectStoreBusy` + send_object_store_busy already
    expresses that correctly (503 + retry_after_seconds). A separate type would have to be added to the same four
    except clauses, and missing one of them shows up on site as the route returning **400** - "storage is unreachable" looks
    to the caller like "your request is malformed", literally the same pit described in the send_store_outage comment.
    The difference between the two only matters for our own troubleshooting, and the message carries it. Not worth another layer of wiring.

    🔴 This type is the **backstop** after /healthz was taken off the readiness probe: previously, once storage went down, the whole
       replica was pulled from Endpoints first and the storage-dependent routes never got a chance to respond; now the replica stays in
       Endpoints, and those routes must give a "retryable" answer themselves."""


#: Per-thread marker: "this thread already holds a queue slot through object_queue_slot".
_OBJECT_GATE_LOCAL = threading.local()


@contextlib.contextmanager
def object_queue_slot() -> Any:
    """Hold a queue slot for a whole request, body included, not only for the store call.

    🔴 Why this exists: the three paths that spool a large body to /tmp (ticket upload, workspace
       export archive, checkpoint) used to read the whole body **first** and only then enter the gate
       inside object_put. SANDBOX_MAX_OBJECT_QUEUE therefore bounded nothing about the spooling itself:
       ThreadingHTTPServer has no thread cap, so N concurrent 64MiB uploads meant N × 64MiB on the tmp
       emptyDir before any of them was refused. The emptyDir has a sizeLimit, and the kubelet's answer
       to exceeding it is to evict the whole Pod - single replica, reaper included.
       Taking the queue slot before the first body byte turns the N+1-th request into a 503 with nothing
       spooled, and makes `MAX_OBJECT_QUEUE × MAX_STREAM_OBJECT_BYTES` the real ceiling on /tmp use,
       which is what the manifests size the volume to.
    Constraint: the store call inside still goes through object_slot, which sees the marker and does not
         take a second queue slot (it would count one request twice and refuse at half the depth); it
         does take the execution slot as usual. object_slot nested in object_slot is **not** made
         re-entrant by this - that is a different situation and must keep refusing."""
    if getattr(_OBJECT_GATE_LOCAL, "queued", False):
        yield
        return
    if not _OBJECT_QUEUE_SLOTS.acquire(blocking=False):
        raise ObjectStoreBusy("object storage is busy; retry shortly")
    _OBJECT_GATE_LOCAL.queued = True
    try:
        yield
    finally:
        _OBJECT_GATE_LOCAL.queued = False
        _OBJECT_QUEUE_SLOTS.release()


@contextlib.contextmanager
def object_slot() -> Any:
    """Enter the object-operation gate. When the queue is full, fail immediately instead of waiting.

    Constraint: the order of the two gates cannot be reversed - take the queue slot first, then the execution slot. The other way round, the thread blocks
         on the execution slot first and the queue slot is useless.
    A thread already queued through object_queue_slot keeps that slot and takes only the execution slot."""
    global _OBJECT_INFLIGHT
    queued_here = not getattr(_OBJECT_GATE_LOCAL, "queued", False)
    if queued_here and not _OBJECT_QUEUE_SLOTS.acquire(blocking=False):
        raise ObjectStoreBusy("object storage is busy; retry shortly")
    with _OBJECT_INFLIGHT_LOCK:
        _OBJECT_INFLIGHT += 1
    try:
        with _OBJECT_SLOTS:
            yield
    finally:
        with _OBJECT_INFLIGHT_LOCK:
            _OBJECT_INFLIGHT -= 1
        if queued_here:
            _OBJECT_QUEUE_SLOTS.release()
_RUNTIME_ADMISSION_LOCK = threading.Lock()
TICKET_LEASE_SELECTOR = "convee.io/purpose=object-ticket"


#/healthz is unauthenticated (see ROUTE_AUTH), so anything that reaches its body
#is readable by anything that can open a TCP connection to the port. A driver or
#socket message can carry the internal address of the thing that failed, which is
#topology disclosure rather than diagnosis. The class of failure is what the
#endpoint is for; the address belongs in the process log, which already needs
#cluster access to read.
_REDACTED = "<redacted>"
_ENDPOINT_PATTERNS = (
    re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE),   # scheme://host/...
    re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),                # bare IPv4
    re.compile(r"\b(?:[A-Za-z0-9_-]+\.)+[A-Za-z]{2,}\b"),       # dotted hostname
)
_MAX_ERROR_CHARS = 200


def redact_endpoints(message: str) -> str:
    """Strip host-shaped substrings from text served on an unauthenticated endpoint.

    Over-redaction is the safe direction: losing a dotted token from an error
    sentence costs a word, keeping one hands out an address. The classification
    in object_store_failure_hint is unaffected - it matches on errno phrases, not
    on the address - so the three-way distinction the caller needs survives.
    """
    for pattern in _ENDPOINT_PATTERNS:
        message = pattern.sub(_REDACTED, message)
    if len(message) > _MAX_ERROR_CHARS:
        message = message[:_MAX_ERROR_CHARS] + "..."
    return message


def object_store_failure_hint(error: BaseException) -> str:
    """Turn a storage probe exception into an actionable verdict.

    Responsibility: classify and give the next action only; no network probing (this path runs inside the readinessProbe,
         and the 2-second budget is already spent by the probe itself).
    Constraint: classification is based on the errno/reason in the exception text, not the exception type - urlopen wraps
         DNS failure, connection refused, and timeout all into URLError, and the types are indistinguishable."""
    detail = str(error)
    if "Name or service not known" in detail or "Try again" in detail:
        return (
            "endpoint name cannot be resolved: the cross-cluster bridge "
            "Service/EndpointSlice is probably gone (Argo CD prune removes "
            "unmanaged objects); verify the configured S3 endpoint or "
            "make infra-refresh on the hub cluster"
        )
    if "Connection refused" in detail or "No route to host" in detail:
        return (
            "endpoint resolved but connection failed: the storage unit is down "
            "or a NetworkPolicy blocks this egress path"
        )
    if "timed out" in detail or isinstance(error, TimeoutError):
        return "connection timed out: storage is overloaded or egress traffic is silently dropped"
    if "not ready" in detail:
        return "endpoint is online but reports not ready: storage is still starting or its disk is unavailable"
    return "unclassified failure; inspect each endpoint hop"


def connection_closed(connection: socket.socket) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return connection.recv(
            1, socket.MSG_PEEK | socket.MSG_DONTWAIT
        ) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True
# The volume role does not touch the Kubernetes API, so it must not mount the ServiceAccount token; and
# KubeClient reads the token file when constructed. Skipping by role means that if this role is mistakenly
# used in code that needs K8s, the error is "NoneType has no attribute" - which points at a role mismatch
# more directly than "cannot read token".
KUBE = None if CONTROL_PLANE_ROLE == "volume" else KubeClient()


def _open_store() -> "Store | None":
    """Open the control-plane store as configured.

    Not configured = single-tenant mode: only the static SANDBOX_CONTROL_PLANE_TOKEN authentication path, behaving exactly as it did
    before multi-tenancy was introduced. This is a deliberate gradual switch - so that deployments that "have not set up a database yet" keep running as usual instead of
    failing hard at startup.

    🔴 Once configured, it sits on the critical path of **creation** (both quota and ownership live in the store). While PG is
    unavailable, Workspaces fail to be created. This is a design trade-off: better not to create than to create while ignoring the quota
    and without recording ownership - such a record could never be reconciled afterwards."""
    if CONTROL_PLANE_ROLE == "volume":
        return None
    backend = os.getenv("SANDBOX_STORE_BACKEND", "").strip().lower()
    if not backend:
        return None
    if backend == "sqlite":
        path = os.getenv("SANDBOX_STORE_PATH", "/tmp/sandbox-control-plane.db")
        return Store.sqlite(path)
    if backend not in {"postgresql", "mysql"}:
        raise SystemExit(
            "control_plane: SANDBOX_STORE_BACKEND must be postgresql, mysql, or sqlite, "
            f"received {backend!r}"
        )
    # Import the driver before touching the network. A driver that is missing
    # from the image is a build defect and must stop the process here, not be
    # reported later as the "store unavailable" warning that means "the
    # database did not answer". The two need different fixes.
    try:
        require_driver(backend)
    except StoreError as exc:
        raise SystemExit(f"control_plane: {exc}") from exc
    password_file = os.getenv(
        "SANDBOX_DB_PASSWORD_FILE", "/var/run/sandbox-db/password"
    )
    try:
        password = pathlib.Path(password_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(
            f"control_plane: cannot read database password {password_file}: {exc}"
        ) from exc
    connect_kwargs = {
        "host": os.getenv("SANDBOX_DB_HOST", "sandbox-postgres"),
        "port": int(os.getenv("SANDBOX_DB_PORT", "5432")),
        "dbname": os.getenv("SANDBOX_DB_NAME", "sandbox"),
        "user": os.getenv("SANDBOX_DB_USER", "sandbox"),
        "password": password,
        "connect_timeout": int(os.getenv("SANDBOX_DB_CONNECT_TIMEOUT", "5")),
    }
    # Every authenticated request passes STORE.authenticate under one process
    # lock, so a statement stuck on FOR UPDATE or a half-open TCP peer would
    # stall the whole API; bound both server-side.
    connect_kwargs.update(
        connection_hardening(
            backend,
            statement_timeout_ms=int(
                os.getenv("SANDBOX_DB_STATEMENT_TIMEOUT_MS", "5000")
            ),
            idle_tx_timeout_ms=int(
                os.getenv("SANDBOX_DB_IDLE_TX_TIMEOUT_MS", "10000")
            ),
        )
    )
    if backend == "mysql":
        connect_kwargs["database"] = connect_kwargs.pop("dbname")
        connect_kwargs["port"] = int(
            os.getenv("SANDBOX_DB_PORT", "3306")
        )
        return Store.mysql(connect_kwargs)
    connect_kwargs["application_name"] = "sandbox-control-plane"
    return Store.postgres(
        connect_kwargs
    )


STORE = _open_store()


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_access_token(
    kind: str,
    subject: str,
    owner: str | None = None,
) -> str:
    """Mint a scoped token, optionally bound to one object owner.

    ``owner`` is what turns a workspace token into an actually reduced
    privilege: without it the holder still names any ``<tenant>/<subject>``
    prefix in an import/export body, so the scoped token grants exactly what
    the admin token does over object storage. It is only ever set from a
    Control Plane-token-authenticated request, never from the scoped caller itself.

    ⚠️ These claims do not carry the id of the key that issued them, so revoking a key cannot invalidate the tokens it
       issued - that batch has to wait for exp to run out naturally (900s by default). Tenant deactivation is a separate route:
       the store is checked again and the next request is blocked (see ApiHandler.scoped_tenant_is_active).
       Immediate revocation would require adding a kid here and checking the store on the verification side; but the static
       SANDBOX_CONTROL_PLANE_TOKEN path has no key id at all, and the tokens it issues could never be covered anyway."""
    claims = {
        "aud": "sandbox-control-plane",
        "kind": kind,
        "sub": subject,
        "exp": int(time.time()) + ACCESS_TOKEN_TTL_SECONDS,
    }
    if owner is not None:
        claims["own"] = validate_object_owner(owner)
    payload = b64url_encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = b64url_encode(
        hmac.new(SIGNING_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{payload}.{signature}"


def verify_access_token(token: str, kind: str, subject: str) -> dict | None:
    """Return the token's claims, or ``None`` when it is not valid here.

    Callers need the claims, not just a verdict: the object owner the token was
    bound to lives in them.
    """
    try:
        payload, supplied_signature = token.split(".", 1)
        expected_signature = b64url_encode(
            hmac.new(
                SIGNING_KEY,
                payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        claims = json.loads(b64url_decode(payload))
        if (
            claims.get("aud") == "sandbox-control-plane"
            and claims.get("kind") == kind
            and claims.get("sub") == subject
            and int(claims.get("exp", 0)) >= int(time.time())
        ):
            return claims
        return None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def scoped_object_owner(claims: dict | None) -> str | None:
    """The owner a scoped token is bound to, if it carries one."""
    if not isinstance(claims, dict) or claims.get("own") is None:
        return None
    return validate_object_owner(claims.get("own"))


def bind_object_owner(payload: dict, scoped_owner: str | None) -> dict:
    """Force an object operation onto the owner its access token was issued for.

    The route already checked that the caller holds a token for *this*
    workspace, but the object prefix comes from the request body — so without
    this the same token can write artifacts into another tenant's prefix, or
    pull another tenant's upload into this workspace.
    """
    if scoped_owner is None:
        # The transition window is closed. This path once fell back to taking the owner from the body, to stay compatible with
        # tokens issued before the owner claim existed - but the token TTL is only 900s (ACCESS_TOKEN_TTL_SECONDS),
        # that batch has long expired, and the fallback itself amounts to giving a delegated token the same rights as an
        # admin token on this path. Both clients of these two routes (import/export) now must pass owner,
        # and ensure_workspace re-signs when the cached lease's owner does not match, so in normal traffic
        # this branch is unreachable; actually reaching it means the token was not signed for object operations, and rejecting is
        # safer than a silent downgrade.
        raise ValueError(
            "workspace access token carries no owner claim; "
            "re-issue it with an owner before touching object storage"
        )
    supplied = payload.get("owner")
    if supplied is not None and str(supplied) != scoped_owner:
        raise ValueError("owner does not match the workspace access token")
    return {**payload, "owner": scoped_owner}


def issue_object_ticket(payload: dict) -> dict:
    operation = payload.get("operation")
    if operation not in {"upload", "download"}:
        raise ValueError("operation must be upload or download")
    bucket, key = object_location(payload)
    try:
        expires_in = int(
            payload.get("expires_in", OBJECT_TICKET_TTL_SECONDS)
        )
        max_bytes = int(
            payload.get("max_bytes", MAX_STREAM_OBJECT_BYTES)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("expires_in and max_bytes must be integers") from exc
    if not 1 <= expires_in <= OBJECT_TICKET_TTL_SECONDS:
        raise ValueError(
            f"expires_in must be 1-{OBJECT_TICKET_TTL_SECONDS} seconds"
        )
    if not 1 <= max_bytes <= MAX_STREAM_OBJECT_BYTES:
        raise ValueError(
            f"max_bytes must be 1-{MAX_STREAM_OBJECT_BYTES}"
        )
    content_type = str(
        payload.get("content_type") or "application/octet-stream"
    ).strip().lower()
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9.+_-]*/[a-z0-9][a-z0-9.+_-]*",
        content_type,
    ):
        raise ValueError("content_type must be a simple MIME type")
    expected_digest = str(payload.get("sha256") or "").lower()
    if expected_digest and not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
        raise ValueError("sha256 must contain 64 lowercase hex characters")
    claims = {
        "aud": "sandbox-control-plane",
        "kind": "object-ticket",
        "op": operation,
        "bucket": bucket,
        "key": key,
        "max_bytes": max_bytes,
        "content_type": content_type,
        "sha256": expected_digest,
        "jti": secrets.token_hex(16),
        "exp": int(time.time()) + expires_in,
    }
    encoded = b64url_encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    signature = b64url_encode(
        hmac.new(SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return {
        "operation": operation,
        "method": "PUT" if operation == "upload" else "GET",
        "url": "/v1/storage/content",
        "access_token": f"{encoded}.{signature}",
        "expires_in": expires_in,
        "max_bytes": max_bytes,
        "object": {"bucket": bucket, "key": key},
    }


def verify_object_ticket(token: str, operation: str) -> dict | None:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = b64url_encode(
            hmac.new(
                SIGNING_KEY,
                encoded.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            OBJECT_TICKET_FAILURES.inc(reason="bad_signature")
            return None
        claims = json.loads(b64url_decode(encoded))
        bucket = claims.get("bucket")
        key = claims.get("key")
        if (
            claims.get("aud") != "sandbox-control-plane"
            or claims.get("kind") != "object-ticket"
            or claims.get("op") != operation
            or int(claims.get("exp", 0)) < int(time.time())
            or bucket not in {OBJECT_STORE_UPLOAD_BUCKET, OBJECT_STORE_AGENT_BUCKET}
            or object_key_owner(key) is None
            or not re.fullmatch(r"[a-f0-9]{32}", str(claims.get("jti") or ""))
            or not 1
            <= int(claims.get("max_bytes", 0))
            <= MAX_STREAM_OBJECT_BYTES
        ):
            OBJECT_TICKET_FAILURES.inc(reason="claims_rejected")
            return None
        return claims
    except (ValueError, TypeError, json.JSONDecodeError):
        OBJECT_TICKET_FAILURES.inc(reason="malformed")
        return None


def consume_object_ticket(claims: dict) -> bool:
    """Atomically consume a signed ticket across Control Plane restarts/threads."""
    jti = str(claims["jti"])
    lease_name = f"ticket-{hashlib.sha256(jti.encode()).hexdigest()[:32]}"
    manifest = {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": lease_name,
            "namespace": SYSTEM_NAMESPACE,
            "labels": {
                "convee.io/purpose": "object-ticket",
                "app.kubernetes.io/managed-by": "sandbox-control-plane",
            },
            "annotations": {
                "convee.io/expires-at": str(int(claims["exp"])),
            },
        },
        "spec": {
            "holderIdentity": jti,
            "leaseDurationSeconds": max(
                1, int(claims["exp"]) - int(time.time())
            ),
        },
    }
    try:
        KUBE.create_group(
            SYSTEM_NAMESPACE,
            "coordination.k8s.io",
            "v1",
            "leases",
            manifest,
        )
        return True
    except KubeError as exc:
        if exc.status == HTTPStatus.CONFLICT:
            # The lease already exists: this ticket was spent. Counted rather
            # than logged because it is the one ticket failure that is normal in
            # small numbers (a client retry) and alarming as a rate.
            OBJECT_TICKET_FAILURES.inc(reason="replayed")
            return False
        raise


def parse_bearer_token(raw: str) -> str:
    """Extract the Bearer credential from the Authorization header. Non-ASCII is always treated as "no credential".

    🔴 Why non-ASCII is blocked explicitly: http.client decodes HTTP headers as iso-8859-1, so
       `Authorization: Bearer` followed by the bytes 0xC3 0xA9 decodes into a **non-ASCII
       str**; and hmac.compare_digest accepts only pure ASCII for str arguments, otherwise it raises
       TypeError.
       No authentication point covered that - the except tuples at the end of the three do_* methods are
       (OSError, RuntimeError, ValueError), without TypeError; do_DELETE and
       VolumeHandler even keep the authentication call **outside** the try. So any unauthenticated client sending a
       non-ASCII Bearer could make the handler thread die abnormally: no 401 response, no
       sandbox_credential_uses_total count, no audit. The last point matters most - that metric is exactly
       the exit criterion for "is the static SANDBOX_CONTROL_PLANE_TOKEN still used by anyone"; if it can be bypassed silently, the criterion is void.

    Boundary: this is not a new rejection rule, it just moves "can never match" forward to "not given". All real credentials
       are pure ASCII - SANDBOX_CONTROL_PLANE_TOKEN / VOLUME_AGENT_TOKEN come from environment variables as hex
       or base64, API keys are generated by secrets.token_urlsafe, and scoped tokens and object
       tickets are base64url. Non-ASCII input can never match anything.
    Boundary: only the value is extracted; whose credential it is stays undecided. The caller still compares against each kind."""
    prefix = "Bearer "
    if not raw.startswith(prefix):
        return ""
    token = raw[len(prefix):]
    return token if token.isascii() else ""


def capability_epoch(kind: str, subject: str) -> int:
    """The epoch currently in force for one sandbox or workspace.

    The verification a sandbox performs already needs its row to exist, so the
    epoch costs no extra lookup on the issuing side either. A missing row is an
    error, not an epoch of 1: minting a ticket for a subject the control plane
    does not know about is the shape of forgery this epoch exists to stop.

    Without a store there are no rows to rotate, so single-tenant deployments
    keep one fixed epoch. Tickets there still expire; only revocation and
    per-instance rotation need the control plane.
    """
    if STORE is None:
        return 1
    if kind == "runtime":
        epoch = STORE.runtime_epoch(subject)
    elif kind == "workspace":
        epoch = STORE.workspace_epoch(subject)
    else:
        raise ValueError(f"unknown capability kind: {kind}")
    if epoch is None:
        raise ValueError(f"unknown {kind}: {subject}")
    return epoch


def capability_key(kind: str, subject: str) -> str:
    """The verification key written into one sandbox instance's environment.

    Never SIGNING_KEY itself: a key read out of a Pod - the namespace of
    untrusted workloads - must not be able to mint anything for any other
    sandbox, and must stop working the moment that instance's epoch moves.
    """
    return capability_ticket.instance_key(
        SIGNING_KEY, kind, subject, capability_epoch(kind, subject)
    )


def capability_ticket_for(kind: str, subject: str) -> str:
    """A short-lived ticket for one internal call into a sandbox."""
    epoch = capability_epoch(kind, subject)
    return capability_ticket.issue(
        capability_ticket.instance_key(SIGNING_KEY, kind, subject, epoch),
        kind,
        subject,
        epoch,
    )


#: Cookie name prefixes this platform issues to browsers, in every form the
#: browser may present them. Stripped before anything is forwarded into a
#: sandbox: a sandbox serves a tenant's own code, and a Console session cookie
#: arriving there is a session hijack that needs no exploit at all. Gitpod,
#: code-server and Daytona each converged on this same rule independently.
PLATFORM_COOKIE_PREFIXES = (
    "sandbox_console_",
    "sandbox_control_",
    "__Host-sandbox_console_",
    "__Host-sandbox_control_",
    "__Secure-sandbox_console_",
    "__Secure-sandbox_control_",
)
#: Request headers that carry this platform's identity, likewise never forwarded.
PLATFORM_HEADERS = frozenset({
    "authorization",
    "cookie",
    "x-sandbox-tenant",
    "x-acting-subject",
    "x-console-csrf",
})


def strip_platform_cookies(raw: str) -> str:
    """Return the caller's Cookie header with this platform's cookies removed.

    🔴 Re-serialized by hand rather than through http.cookies. SimpleCookie
    re-encodes values on output, so a cookie the tenant's own application set
    would come back quoted or percent-escaped and the application would read a
    different value than it wrote. Splitting on the wire separator keeps every
    surviving pair byte for byte as it arrived.
    """
    kept = []
    for pair in raw.split(";"):
        candidate = pair.strip()
        if not candidate:
            continue
        name = candidate.split("=", 1)[0].strip()
        if name.startswith(PLATFORM_COOKIE_PREFIXES):
            continue
        kept.append(candidate)
    return "; ".join(kept)


def forwardable_headers(headers: Any, allowed: tuple[str, ...]) -> dict[str, str]:
    """The subset of an inbound request's headers that may cross into a sandbox.

    An allow list, plus the caller's own cookies with this platform's removed.
    A deny list would be wrong here: the next header this platform invents would
    be forwarded by default, and nothing would notice.
    """
    forwarded: dict[str, str] = {}
    for name in allowed:
        if name.lower() in PLATFORM_HEADERS:
            raise ValueError(f"{name} is a platform header and cannot be forwarded")
        value = headers.get(name)
        if value:
            forwarded[name] = value
    cookies = strip_platform_cookies(str(headers.get("Cookie", "")))
    if cookies:
        forwarded["Cookie"] = cookies
    return forwarded


_OBJECT_STORE_CLIENT: Any = None
_OBJECT_STORE_CLIENT_LOCK = threading.Lock()


def object_store() -> Any:
    """The S3 client, built once.

    This used to shell out to the MinIO Client, which meant writing an alias
    config file with the credentials in it and forking a Go binary per
    operation. mc is AGPL-3.0; boto3 is Apache-2.0 and is what the rest of this
    platform already uses.
    """
    global _OBJECT_STORE_CLIENT
    if _OBJECT_STORE_CLIENT is not None:
        return _OBJECT_STORE_CLIENT
    if not OBJECT_STORE_ENDPOINT.startswith(("http://", "https://")):
        raise ValueError("object storage endpoint is invalid")
    with _OBJECT_STORE_CLIENT_LOCK:
        if _OBJECT_STORE_CLIENT is None:
            _OBJECT_STORE_CLIENT = boto3.client(
                "s3",
                endpoint_url=OBJECT_STORE_ENDPOINT,
                aws_access_key_id=OBJECT_STORE_ACCESS_KEY,
                aws_secret_access_key=OBJECT_STORE_SECRET_KEY,
                config=BotoConfig(
                    signature_version=(
                        "s3v4"
                        if OBJECT_STORE_SIGNATURE_VERSION == "S3V4"
                        else "s3"
                    ),
                    # boto3 1.36 began adding x-amz-checksum-crc32 and
                    # x-amz-sdk-checksum-algorithm to every upload, and signing
                    # them. README says this needs an S3-compatible store, not
                    # AWS; older Ceph RGW and MinIO answer 400 InvalidRequest or
                    # 501 to those headers, which would fail every upload -- and
                    # a 400 classifies as "the request was rejected", sending
                    # the caller off to fix a request that is fine. mc never
                    # sent them, so this is a compatibility risk the swap would
                    # have introduced rather than one that was already there.
                    request_checksum_calculation="when_required",
                    s3={"addressing_style": OBJECT_STORE_ADDRESSING_STYLE},
                    # One connection per in-flight operation, matched to the
                    # semaphore rather than left at botocore's default of 10:
                    # a pool wider than the gate can only hold sockets open.
                    max_pool_connections=MAX_CONCURRENT_OBJECT_OPS,
                    connect_timeout=10,
                    read_timeout=60,
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
    return _OBJECT_STORE_CLIENT


# The store expresses both "endpoint out of reach" and "this key does not exist" as an
# exception; the two misjudgment directions cost differently. Judging "object does not
# exist" as an outage makes the caller retry a few times needlessly; judging "the endpoint
# is down" as a bad request makes the caller rotate its request and credentials when all it
# should do is wait.
#
# This used to substring-match mc's stderr, and the comment that stood here admitted the
# gap: RGW answering 503 while the cluster is degraded produced wording nobody had captured,
# so it landed in "rejected" - the expensive direction. botocore reports the status code, so
# a 5xx is now classified from the response rather than from prose.
#: How many rows a listing may return before it is refused. The mc path had a
#: byte ceiling per invocation -- 2 MiB for a listing, 8 MiB for the reaper's
#: sweep -- and dropping the subprocess dropped the ceiling with it. Nothing
#: else bounds a listing: `read_timeout` is per socket read, so a slow trickle
#: resets it forever while holding the single operation slot and growing the
#: list in memory. These are the old byte limits at roughly 200 bytes per
#: entry, which is what mc's JSON lines measured.
MAX_LIST_ENTRIES = int(os.getenv("SANDBOX_MAX_LIST_ENTRIES", "10000"))
if MAX_LIST_ENTRIES <= 0:
    raise ValueError("SANDBOX_MAX_LIST_ENTRIES must be greater than zero")

_OUTAGE_STATUS = frozenset({500, 502, 503, 504})
#: Transport failures: the request never got an answer. Named rather than
#: inlined so a test can assert every one of them has a sample -- a branch
#: nobody ever fed a matching value to is indistinguishable from one that
#: cannot match anything.
_OUTAGE_EXCEPTIONS = (
    EndpointConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
    ConnectionClosedError,
    # Raised while a body is being consumed rather than while the request is
    # being made. A download that dies halfway is the storage going out of
    # reach, not the request being refused.
    ResponseStreamingError,
    IncompleteReadError,
)


def failure_is_outage(error: BaseException) -> bool:
    """Whether this failure means "the storage is out of reach" or "this operation was rejected".

    Responsibility: classification only; no retries, no probing.
    Constraint: **the store's own message never reaches the caller** - it is external
         text and carries the endpoint address and bucket names."""
    if isinstance(error, ClientError):
        status = (
            error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        )
        return status in _OUTAGE_STATUS
    return isinstance(error, _OUTAGE_EXCEPTIONS)


def _translate(error: BaseException) -> BaseException:
    if failure_is_outage(error):
        return ObjectStoreUnavailable(
            "object storage is unreachable; retry shortly"
        )
    return RuntimeError("object storage rejected the operation")


def object_call(operation: str, action: Any, *args: Any, **kwargs: Any) -> Any:
    """Run one object-store call inside the gate, with tracing and metrics.

    `operation` is the coarse verb the metric is labelled with - the same set
    the mc argv used to be mapped to, so the series is continuous across this
    change.
    """
    with tracing.start_span(
        "object_store.operation",
        kind=3,
        attributes={"sandbox.object_store.operation": operation},
    ):
        try:
            with object_slot():
                result = action(*args, **kwargs)
        except (ObjectStoreBusy, ObjectStoreUnavailable):
            OBJECT_STORE_OPERATIONS.inc(operation=operation, outcome="error")
            raise
        except (ClientError, BotoCoreError, OSError) as error:
            OBJECT_STORE_OPERATIONS.inc(operation=operation, outcome="error")
            raise _translate(error) from error
        except Exception:
            OBJECT_STORE_OPERATIONS.inc(operation=operation, outcome="error")
            raise
        OBJECT_STORE_OPERATIONS.inc(operation=operation, outcome="success")
        return result


def object_put(
    bucket: str,
    key: str,
    body: Any,
    *,
    content_type: str | None = None,
    metadata: dict[str, str] | None = None,
) -> None:
    extra: dict[str, Any] = {}
    if content_type is not None:
        extra["ContentType"] = content_type
    if metadata:
        extra["Metadata"] = metadata
    object_call(
        "write",
        object_store().put_object,
        Bucket=bucket,
        Key=key,
        Body=body,
        **extra,
    )


def object_get(
    bucket: str,
    key: str,
    max_bytes: int,
    *,
    expected_sha256: str | None = None,
) -> bytes:
    """Read one object whole, refusing anything that is not provably the whole object.

    `expected_sha256` is the caller's own record of the content (checkpoint metadata, a ticket); it is
    the only thing that can vouch for a body the store did not declare a length for."""
    def read() -> bytes:
        response = object_store().get_object(Bucket=bucket, Key=key)
        # Not `with response["Body"]`: StreamingBody.__enter__ returns its
        # _raw_stream, so the `with` form hands back urllib3's response object
        # and reads on it raise urllib3 exceptions -- ProtocolError,
        # ReadTimeoutError, IncompleteRead -- none of which is a ClientError, a
        # BotoCoreError or an OSError. They pass through every handler here and
        # in api.py and reach the socket server as an unhandled exception.
        stream = response["Body"]
        try:
            # One byte past the ceiling, so "exactly at the limit" and "over it"
            # stay distinguishable at the call site.
            data = stream.read(max_bytes + 1)
        finally:
            stream.close()
        if len(data) > max_bytes:
            raise ValueError("object storage response is too large")
        # A truncated body is not a short object. urllib3 only raises
        # IncompleteRead when a read returns nothing at all, so a connection cut
        # mid-object hands back the prefix and no error -- and get_object()
        # hashes that prefix and returns a self-consistent, wrong answer.
        # `mc cat` used to catch this with its exit code.
        #
        # 🔴 The comparison must hold for a declared length **above** the ceiling
        # too. The read asks for max_bytes + 1, so an object declared larger than
        # that is expected to deliver exactly max_bytes + 1 bytes (and be refused
        # above as too large); delivering fewer means the connection died before
        # the ceiling, and the prefix must not come back as a legal small object.
        # The earlier `declared <= max_bytes and` guard skipped exactly that case.
        declared = response.get("ContentLength")
        if declared is None:
            # No length to check against: only the caller's own digest can vouch
            # for the body. Without one, refuse rather than trust a chunked body.
            if expected_sha256 is None:
                raise ObjectStoreUnavailable(
                    "object storage is unreachable; retry shortly"
                )
            if not hmac.compare_digest(
                hashlib.sha256(data).hexdigest(), expected_sha256
            ):
                raise ObjectStoreUnavailable(
                    "object storage is unreachable; retry shortly"
                )
            return data
        expected = min(int(declared), max_bytes + 1)
        if len(data) != expected:
            raise ObjectStoreUnavailable(
                "object storage is unreachable; retry shortly"
            )
        return data

    return object_call("read", read)


def object_list(bucket: str, prefix: str) -> list[dict[str, Any]]:
    def listing() -> list[dict[str, Any]]:
        paginator = object_store().get_paginator("list_objects_v2")
        items: list[dict[str, Any]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                items.append(
                    {
                        "key": item["Key"],
                        "bytes": int(item.get("Size") or 0),
                        "last_modified": _timestamp(item.get("LastModified")),
                    }
                )
            if len(items) > MAX_LIST_ENTRIES:
                # Checked between pages, not after: the point is to stop
                # fetching, not to discover afterwards how much was fetched.
                raise ValueError("object storage listing is too large")
        return items

    return object_call("list", listing)


def object_list_page(
    bucket: str,
    prefix: str,
    *,
    continuation_token: str | None = None,
    page_size: int = 1000,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of a listing, and the token for the next one (None on the last page).

    Responsibility: the bounded form of object_list for callers that walk a prefix of unknown
         size and act on each page as it arrives - the checkpoint GC sweeps the whole bucket.
    🔴 Why not object_list: it accumulates and refuses past MAX_LIST_ENTRIES. That is right for a
       response body, and wrong for a sweep whose job is to make the count go down: once the bucket
       held more checkpoint objects than the ceiling, every GC round raised before deleting anything,
       and nothing else ever removed them - the sweep stopped for good and the bucket only grew.
       A page holds the gate for one round trip and at most page_size rows; memory is bounded by the
       page, not by the bucket.
    Constraint: page_size is capped by MAX_LIST_ENTRIES so the per-call ceiling stays the same."""
    if page_size < 1:
        raise ValueError("page_size must be positive")
    page_size = min(page_size, MAX_LIST_ENTRIES)

    def listing() -> tuple[list[dict[str, Any]], str | None]:
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": prefix,
            "MaxKeys": page_size,
        }
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        page = object_store().list_objects_v2(**kwargs)
        items = [
            {
                "key": item["Key"],
                "bytes": int(item.get("Size") or 0),
                "last_modified": _timestamp(item.get("LastModified")),
            }
            for item in page.get("Contents") or []
        ]
        next_token = (
            page.get("NextContinuationToken") if page.get("IsTruncated") else None
        )
        return items, (str(next_token) if next_token else None)

    return object_call("list", listing)


def object_versions(bucket: str, key: str) -> list[dict[str, Any]]:
    """Every version and delete marker of one key, newest first.

    `version_ordinal` has no S3 equivalent -- it was mc's own numbering, and it
    is kept because it is part of the response shape. It is derived from
    modification time so the invariant it exists for, highest ordinal is the
    current version, still holds. `is_latest` comes from the store rather than
    from the ordinal: S3 reports it, and deriving it here would be a second
    opinion that can disagree with the first.
    """
    def listing() -> list[dict[str, Any]]:
        paginator = object_store().get_paginator("list_object_versions")
        rows: list[dict[str, Any]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=key):
            for item in page.get("Versions") or []:
                if item.get("Key") != key:
                    continue
                rows.append(_version_row(item, delete_marker=False))
            for item in page.get("DeleteMarkers") or []:
                if item.get("Key") != key:
                    continue
                rows.append(_version_row(item, delete_marker=True))
            if len(rows) > MAX_LIST_ENTRIES:
                raise ValueError("object storage listing is too large")
        # Oldest first while numbering. A row with no timestamp sorts before
        # every dated one rather than raising: datetime and None are not
        # comparable, and one undated row would otherwise take out the listing.
        dated = [row for row in rows if row["_sort_key"] is not None]
        undated = [row for row in rows if row["_sort_key"] is None]
        dated.sort(key=lambda row: row["_sort_key"])
        ordered = undated + dated
        for position, row in enumerate(ordered, start=1):
            row["version_ordinal"] = position
            row.pop("_sort_key", None)
        ordered.reverse()
        return ordered

    return object_call("list", listing)


@contextlib.contextmanager
def object_stream(bucket: str, key: str) -> Any:
    """Hold the gate and yield the object body for streaming to a client.

    Separate from object_get because the caller writes the bytes out as they
    arrive rather than buffering the whole object: this is the path a download
    ticket takes, and the size limit is the ticket's, checked before the body
    is opened.
    """
    with tracing.start_span(
        "object_store.operation",
        kind=3,
        attributes={"sandbox.object_store.operation": "read"},
    ):
        with object_slot():
            try:
                response = object_store().get_object(Bucket=bucket, Key=key)
            except (ClientError, BotoCoreError, OSError) as error:
                OBJECT_STORE_OPERATIONS.inc(operation="read", outcome="error")
                raise _translate(error) from error
            # See object_get: the `with` form yields urllib3's raw stream and
            # its exceptions escape every handler between here and the socket.
            body = response["Body"]
            try:
                yield body
            except (ClientError, BotoCoreError, OSError) as error:
                OBJECT_STORE_OPERATIONS.inc(operation="read", outcome="error")
                raise _translate(error) from error
            except Exception:
                OBJECT_STORE_OPERATIONS.inc(operation="read", outcome="error")
                raise
            finally:
                body.close()
            OBJECT_STORE_OPERATIONS.inc(operation="read", outcome="success")


def object_head(bucket: str, key: str) -> dict[str, Any]:
    return object_call("metadata", object_store().head_object, Bucket=bucket, Key=key)


def object_delete(bucket: str, key: str) -> None:
    object_call("delete", object_store().delete_object, Bucket=bucket, Key=key)


def object_delete_versions(bucket: str, key: str) -> None:
    """Delete the object and every version and delete marker it has.

    `mc rm --versions --force` in one call; there is no single S3 verb for it.
    """
    def purge() -> None:
        store = object_store()
        paginator = store.get_paginator("list_object_versions")
        targets: list[dict[str, str]] = []
        for page in paginator.paginate(Bucket=bucket, Prefix=key):
            for group in ("Versions", "DeleteMarkers"):
                for item in page.get(group) or []:
                    if item.get("Key") != key:
                        continue
                    targets.append(
                        {"Key": item["Key"], "VersionId": item["VersionId"]}
                    )
            if len(targets) > MAX_LIST_ENTRIES:
                raise ValueError("object storage listing is too large")
        if not targets:
            store.delete_object(Bucket=bucket, Key=key)
            return
        for start in range(0, len(targets), 1000):
            store.delete_objects(
                Bucket=bucket,
                Delete={"Objects": targets[start:start + 1000], "Quiet": True},
            )

    object_call("delete", purge)


def _timestamp(value: Any) -> str | None:
    """The listing timestamp, as the string the mc-shaped responses carried."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def _version_row(item: dict[str, Any], *, delete_marker: bool) -> dict[str, Any]:
    return {
        "version_id": item.get("VersionId"),
        "is_latest": bool(item.get("IsLatest")),
        "delete_marker": delete_marker,
        "bytes": int(item.get("Size") or 0),
        "last_modified": _timestamp(item.get("LastModified")),
        "etag": item.get("ETag"),
        "_sort_key": item.get("LastModified"),
    }


def validate_object_id(raw_value: object, field: str) -> str:
    value = str(raw_value or "").strip().lower()
    if not OBJECT_ID.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase DNS-style identifier")
    return value


def validate_object_owner(raw_value: object) -> str:
    """Validate the ``<tenant>/<subject>`` partition every object key carries.

    This is the only guard between a caller-supplied string and the key sent
    to the store.  A leading alphanumeric already makes a dot-only segment
    impossible, but ``users/../../x`` escapes its prefix the moment any layer
    normalizes the path, so that case is also rejected explicitly rather than
    resting on one character class staying the way it is.
    """
    owner = str(raw_value or "")
    if not OBJECT_OWNER.fullmatch(owner):
        raise ValueError(
            "owner must be <tenant>/<subject>, each 1-128 characters starting "
            "with a letter or digit and using letters, digits, dot, at, "
            "underscore or hyphen"
        )
    if any(set(segment) == {"."} for segment in owner.split("/")):
        raise ValueError("owner segments must not consist of dots")
    return owner


def object_key_owner(key: object) -> str | None:
    """Owner of a Control Plane-built object key, or ``None`` when it is not one."""
    if not isinstance(key, str):
        return None
    parts = key.split("/")
    if (
        len(parts) < 5
        or parts[0] != "users"
        or parts[3] not in OBJECT_OWNER_ROOTS
    ):
        return None
    try:
        return validate_object_owner(f"{parts[1]}/{parts[2]}")
    except ValueError:
        return None


def validate_object_path(
    raw_path: object,
    *,
    allowed_roots: set[str],
    allow_root: bool = False,
) -> str:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ValueError("object path is required")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("object path escapes its prefix")
    normalized = str(path)
    if normalized == "." and not allow_root:
        raise ValueError("object path must name an object")
    if len(normalized.encode("utf-8")) > 512:
        raise ValueError("object path is too long")
    if normalized != "." and path.parts[0] not in allowed_roots:
        allowed = ", ".join(sorted(allowed_roots))
        raise ValueError(f"object path must start with one of: {allowed}")
    return normalized


def object_location(
    payload: dict,
    *,
    allow_prefix: bool = False,
) -> tuple[str, str]:
    owner = validate_object_owner(payload.get("owner"))
    scope = payload.get("scope")
    if scope == "upload":
        upload_id = validate_object_id(payload.get("upload_id"), "upload_id")
        path = validate_object_path(
            payload.get("path", "."),
            allowed_roots={"source", "derived", "meta"},
            allow_root=allow_prefix,
        )
        prefix = f"users/{owner}/uploads/{upload_id}"
        return (
            OBJECT_STORE_UPLOAD_BUCKET,
            prefix if path == "." else f"{prefix}/{path}",
        )
    if scope == "agent":
        agent_id = validate_object_id(payload.get("agent_id"), "agent_id")
        run_id = validate_object_id(payload.get("run_id"), "run_id")
        path = validate_object_path(
            payload.get("path", "."),
            allowed_roots={"inputs", "outputs", "artifacts", "logs", "meta"},
            allow_root=allow_prefix,
        )
        prefix = f"users/{owner}/agents/{agent_id}/runs/{run_id}"
        return (
            OBJECT_STORE_AGENT_BUCKET,
            prefix if path == "." else f"{prefix}/{path}",
        )
    raise ValueError("scope must be upload or agent")


def validate_workspace_transfer_path(raw_path: object, root: str) -> str:
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise ValueError("workspace path is required")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("workspace path escapes /workspace")
    if not path.parts or path.parts[0] != root:
        raise ValueError(f"workspace path must start with {root}/")
    normalized = str(path)
    if normalized == root:
        raise ValueError("workspace path must name a file")
    if len(normalized.encode("utf-8")) > 512:
        raise ValueError("workspace path is too long")
    return normalized


def put_object(payload: dict) -> dict:
    bucket, key = object_location(payload)
    encoded = payload.get("content_base64")
    if not isinstance(encoded, str):
        raise ValueError("content_base64 must be a string")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("content_base64 is invalid") from exc
    if len(data) > MAX_OBJECT_BYTES:
        raise ValueError("object is too large")
    digest = hashlib.sha256(data).hexdigest()
    expected_digest = payload.get("sha256")
    if expected_digest is not None and not hmac.compare_digest(
        str(expected_digest), digest
    ):
        raise ValueError("sha256 does not match content")
    object_put(bucket, key, data)
    return {
        "scope": payload["scope"],
        "bucket": bucket,
        "key": key,
        "bytes": len(data),
        "sha256": digest,
    }


def put_object_bytes(
    payload: dict,
    data: bytes,
    *,
    max_bytes: int = MAX_OBJECT_BYTES,
) -> dict:
    """Store trusted internal bytes without base64-expanding them in memory."""
    bucket, key = object_location(payload)
    if not isinstance(data, bytes):
        raise ValueError("object content must be bytes")
    if not 1 <= max_bytes <= MAX_STREAM_OBJECT_BYTES:
        raise ValueError("invalid object byte limit")
    if not data or len(data) > max_bytes:
        raise ValueError("object is empty or too large")
    digest = hashlib.sha256(data).hexdigest()
    expected_digest = payload.get("sha256")
    if expected_digest is not None and not hmac.compare_digest(
        str(expected_digest), digest
    ):
        raise ValueError("sha256 does not match content")
    # The queue slot is taken before anything touches /tmp; see object_queue_slot.
    with object_queue_slot(), tempfile.SpooledTemporaryFile(
        max_size=1024 * 1024,
        mode="w+b",
        dir="/tmp",
    ) as handle:
        handle.write(data)
        handle.seek(0)
        object_put(bucket, key, handle)
    return {
        "scope": payload["scope"],
        "bucket": bucket,
        "key": key,
        "bytes": len(data),
        "sha256": digest,
    }


def get_object(query: dict) -> dict:
    bucket, key = object_location(query)
    data = object_get(bucket, key, MAX_OBJECT_BYTES)
    if len(data) > MAX_OBJECT_BYTES:
        raise ValueError("object is too large")
    return {
        "scope": query["scope"],
        "bucket": bucket,
        "key": key,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def list_objects(query: dict) -> dict:
    bucket, prefix = object_location(query, allow_prefix=True)
    objects = object_list(bucket, prefix)
    return {"scope": query["scope"], "bucket": bucket, "objects": objects}


def stat_object(query: dict) -> dict:
    bucket, key = object_location(query)
    item = object_stat(bucket, key)
    metadata = item.get("metadata") or {}
    return {
        "scope": query["scope"],
        "bucket": bucket,
        "key": key,
        "bytes": int(item.get("size") or 0),
        "etag": item.get("etag"),
        "last_modified": item.get("lastModified"),
        "version_id": item.get("versionID"),
        "content_type": metadata.get(
            "Content-Type", "application/octet-stream"
        ),
        "sha256": metadata.get("X-Amz-Meta-Sha256"),
    }


def object_stat(bucket: str, key: str) -> dict:
    """HEAD, shaped the way the mc `stat --json` output was consumed.

    The keys stay as they were so stat_object below is unchanged; the values
    now come from response fields instead of parsed JSON text.
    """
    head = object_head(bucket, key)
    return {
        "size": int(head.get("ContentLength") or 0),
        "etag": head.get("ETag"),
        "lastModified": _timestamp(head.get("LastModified")),
        "versionID": head.get("VersionId"),
        "metadata": {
            "Content-Type": head.get(
                "ContentType", "application/octet-stream"
            ),
            # S3 returns user metadata lower-cased and without the header
            # prefix; mc surfaced the header name. Both spellings are accepted
            # so a checkpoint written before this change still reads back.
            "X-Amz-Meta-Sha256": (head.get("Metadata") or {}).get("sha256")
            or (head.get("Metadata") or {}).get("Sha256"),
        },
    }


def list_object_versions(query: dict) -> dict:
    bucket, key = object_location(query)
    versions = object_versions(bucket, key)
    return {
        "scope": query["scope"],
        "bucket": bucket,
        "key": key,
        "versions": versions,
    }


def delete_object(query: dict) -> dict:
    bucket, key = object_location(query)
    purge_versions = str(query.get("purge_versions") or "").lower() in {
        "1", "true", "yes",
    }
    if purge_versions:
        object_delete_versions(bucket, key)
    else:
        object_delete(bucket, key)
    return {
        "scope": query["scope"],
        "bucket": bucket,
        "key": key,
        "deleted": True,
        "history_retained": not purge_versions,
    }


def escape_derivation_field(value: str) -> str:
    """Encode a free-text field into a form that contains no bare ':', and the encoding is **injective**.

        Serves only the derivation material of workspace_id_for_session. The only
        required property is **injectivity** — two different field tuples must not
        produce the same material string. Both the separator and the escape
        character must be escaped; miss one and the encoding is irreversible.
        Escaping the backslash first is the conventional order, so the resulting
        string can be decoded left-to-right under the usual rules.

        Backward compatibility: values containing neither `\\` nor `:` are
        returned unchanged, so the IDs of the vast majority of existing Workspaces
        do not move. What does move is exactly the set that is already ambiguous
        today (see the 🔴 at the call site).
    """
    return value.replace("\\", "\\\\").replace(":", "\\:")


def workspace_id_for_session(
    session_id: str,
    *,
    tenant_id: str | None = None,
    principal_kind: str = "",
    principal_id: str = "",
) -> str:
    """Derive a Workspace ID from the session identity. One-way, fixed-length, and does not leak the identity itself.

        🔴 Two derivation materials coexist, and this **must** be so:
          - Single tenant (empty tenant_id): keep `workspace:<session>`. Changing it
            would change the IDs of all existing Workspaces — the same session coming
            back would point to an empty directory, while the old directory is left
            unclaimed on the volume.
          - Multi-tenant: `ws:v2:<tenant>:<kind>:<principal>:<session>`. The version
            prefix cannot be omitted: without a version bump the two materials land in
            the same namespace, and the collision manifests as "two tenants sharing
            one Workspace", the worst kind of cross-tenant mix-up.

        🔴 The separator must be escaped, otherwise the concatenation is not
           injective. tenant_id is constrained by TENANT_ID to contain no ':', so
           **cross-tenant** collision does not hold (the previous point is not
           weakened); but principal_kind / principal_id / session_id are all free
           text (the store's FREEFORM only blocks control characters and
           over-length), so ("alice:s1", "x") and ("alice", "s1:x") would
           concatenate into the same material and derive the same Workspace. The
           consequence is more than a "collision": admit_workspace sees the same
           workspace_id within the same tenant and reports WORKSPACE_REUSED
           (idempotent re-entry), so the second principal gets the access_token of
           the first principal's Workspace — cross-principal read/write mix-up within
           the tenant. So this is an intra-tenant problem that does not cross the
           Control Plane's trust boundary, but it is still data mix-up.

        Boundary: escaping **does not change the version prefix**, intentionally.
           Bumping to v3 would change the IDs of all existing multi-tenant Workspaces
           at once (equivalent to discarding them all), while escaping only moves the
           small set whose values actually contain ':' or '\\' — exactly the set
           that may currently be shared by two principals. Residual theoretical risk:
           under the new scheme the material encoded from ("alice:s1") could collide
           with a tuple whose id happens to end with a backslash under the old scheme.
           For that to collide, someone must first use a backslash at the end of
           principal_id, a cost far smaller than discarding everything.
    """
    if not tenant_id:
        material = f"workspace:{session_id}"
    else:
        material = ":".join(
            (
                "ws",
                "v2",
                escape_derivation_field(tenant_id),
                escape_derivation_field(principal_kind),
                escape_derivation_field(principal_id),
                escape_derivation_field(session_id),
            )
        )
    digest = hmac.new(
        WORKSPACE_ID_KEY, material.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"ws-{digest[:12]}"


def parse_principal(
    payload: dict, acting_subject: str | None = None
) -> tuple[str, str]:
    """Read the principal this request is for.

    🔴 An authorized X-Acting-Subject wins over anything in the body, and the
    two may not both be given. A body principal is free text the caller picks;
    the header is a pseudonym the caller was granted permission to use. Letting
    a request carry both means the record says one thing and the permission
    check looked at the other.

    kind is an **open enumeration** - user / agent / service / job is up to the tenant; the Control Plane
    assigns it no semantics and uses it only as derivation material and an index dimension. This is where "generic" lands: adding a dimension costs
    the Control Plane zero changes.
    The defaults let callers that do not care about this dimension (including the agent host during the transition) keep working as before."""
    principal = payload.get("principal")
    if acting_subject:
        if principal is not None:
            raise ValueError(
                "principal cannot be combined with X-Acting-Subject"
            )
        return "subject", acting_subject
    if principal is None:
        return "service", "default"
    if not isinstance(principal, dict):
        raise ValueError("principal must be an object")
    kind = principal.get("kind", "service")
    identifier = principal.get("id", "default")
    if not isinstance(kind, str) or not isinstance(identifier, str):
        raise ValueError("principal.kind and principal.id must be strings")
    if not kind or not identifier:
        raise ValueError("principal.kind and principal.id must be non-empty")
    if len(kind) > 128 or len(identifier) > 128:
        raise ValueError("principal fields must be at most 128 characters")
    return kind, identifier


# Local Workspace access when Runtime is unavailable. Aliases retain the
# internal names while values come from Runtime's shared contract.
LOCAL_MAX_LIST_ENTRIES = workspace_contract.MAX_LIST_ENTRIES
LOCAL_MAX_READ_SOURCE_BYTES = workspace_contract.MAX_READ_SOURCE_BYTES
LOCAL_MAX_READ_LINES = workspace_contract.MAX_READ_LINES
LOCAL_MAX_READ_CHARS = workspace_contract.MAX_READ_CHARS
LOCAL_MAX_FILE_BYTES = workspace_contract.MAX_FILE_BYTES


class WorkspaceOffline(RuntimeError):
    """The Runtime is absent, and this operation has no local implementation.

    The distinction from KubeError(404) is intentional: 404 is "this Workspace does not exist", while this one is
    "it exists, there is just no compute to execute it right now". The caller decides on that basis whether to report an error or to bring up a
    Runtime first; mixed together, it could only guess."""




def runtime_pod_name(sandbox_id: str) -> str:
    """Compatibility helper; new orchestration uses RuntimeDriver.resource_name."""
    return f"runtime-{sandbox_id}"


def runtime_manifest_settings() -> manifests.ManifestSettings:
    """Build the provider configuration passed to the active Runtime Driver."""
    return manifests.ManifestSettings(
        workload_namespace=WORKLOAD_NAMESPACE,
        workspace_pvc=WORKSPACE_PVC,
        workspace_storage_mode=WORKSPACE_STORAGE_MODE,
        runtime_class=SANDBOX_RUNTIME_CLASS,
        runtime_node_selector=SANDBOX_RUNTIME_NODE_SELECTOR,
        runtime_tolerations=SANDBOX_RUNTIME_TOLERATIONS,
        runtime_ttl_seconds=SANDBOX_TTL_SECONDS,
        runtime_hard_ttl_seconds=RUNTIME_HARD_TTL_SECONDS,
        runtime_name=runtime_pod_name,
        template_image=template_image,
        capability_key=capability_key,
        capability_epoch=capability_epoch,
    )


def configured_runtime_driver() -> RuntimeDriver:
    """Return the only Runtime Driver supported by this release.

    Constructing it from the current composition-root dependencies keeps tests
    and future controller extraction free to inject a Kubernetes client.  No
    backend selector exists yet: accepting a name other than gVisor before an
    implementation is shipped would be a fail-open configuration surface.
    """
    return GVisorRuntimeDriver(KUBE, runtime_manifest_settings())


def runtime_endpoint(sandbox_id: str) -> str:
    return configured_runtime_driver().endpoint(sandbox_id)


def wait_for_runtime(sandbox_id: str, timeout: float = 90.0) -> RuntimeInstance:
    """Wait for a Runtime through the active Driver."""
    deadline = time.monotonic() + timeout
    last_status = "Pending"
    driver = configured_runtime_driver()
    while time.monotonic() < deadline:
        try:
            runtime = driver.get_runtime(sandbox_id)
        except RuntimeDriverError as exc:
            if exc.code == RuntimeDriverErrorCode.NOT_FOUND:
                time.sleep(0.25)
                continue
            raise
        last_status = runtime.state
        if runtime.ready:
            return runtime
        if last_status == "failed":
            raise RuntimeError(
                f"Runtime {sandbox_id} failed: "
                f"{runtime.message or 'unknown error'}"
            )
        # Kubernetes readiness may change between two API reads. A short,
        # bounded interval avoids adding another half-second to every cold
        # start without busy-spinning against the API server.
        time.sleep(0.25)
    raise TimeoutError(
        f"Runtime {sandbox_id} was not ready; last phase={last_status}"
    )


# Query keys the Control Plane relays to a workspace File Service. An allowlist
# rather than a passthrough: the File Service validates every one of these,
# and anything unrecognised should not reach it in the first place.
FORWARDED_FILE_QUERY_KEYS = (
    "path", "offset", "limit", "pattern", "glob", "mode",
    "case_insensitive", "context", "regex",
)


def forwarded_query(query: dict[str, list[str]]) -> dict[str, str]:
    forwarded = {"path": query.get("path", [""])[0]}
    for key in FORWARDED_FILE_QUERY_KEYS:
        values = query.get(key)
        if values:
            forwarded[key] = values[0]
    return forwarded


def internal_http(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    body_bytes: bytes | None = None,
    timeout: float = 40,
) -> tuple[int, bytes, str]:
    parsed_url = urlparse(url)
    with tracing.start_span(
        "internal.http.request",
        kind=3,
        attributes={
            "http.request.method": method,
            "server.address": parsed_url.hostname or "unknown",
        },
    ) as span:
        result = _internal_http(
            method,
            url,
            token,
            payload,
            query,
            headers,
            body_bytes,
            timeout,
        )
        span.set_attribute("http.response.status_code", int(result[0]))
        return result


def _internal_http(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    body_bytes: bytes | None = None,
    timeout: float = 40,
) -> tuple[int, bytes, str]:
    if query:
        url = f"{url}?{urlencode(query)}"
    if payload is not None and body_bytes is not None:
        raise ValueError("payload and body_bytes are mutually exclusive")
    body = (
        body_bytes
        if body_bytes is not None
        else json.dumps(payload).encode("utf-8")
        if payload is not None
        else None
    )
    request_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        # Continues the caller's trace across the hop. A new span id per hop is
        # what makes the hops distinguishable; the trace id is carried through
        # unchanged. Empty outside a request, so background work does not claim
        # to belong to a trace nobody started.
        **tracing.outbound_headers(),
    }
    if body is not None:
        request_headers["Content-Type"] = (
            "application/octet-stream" if body_bytes is not None else "application/json"
        )
    if headers:
        request_headers.update(headers)
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return (
                response.status,
                response.read(),
                response.headers.get("Content-Type", "application/json"),
            )
    except HTTPError as exc:
        return (
            exc.code,
            exc.read(),
            exc.headers.get("Content-Type", "application/json"),
        )
    except (OSError, TimeoutError, URLError) as exc:
        body = json.dumps(
            {"error": f"internal service unavailable: {exc}"}
        ).encode("utf-8")
        return HTTPStatus.BAD_GATEWAY, body, "application/json"


def wait_for_internal_health(
    url: str,
    token: str,
    *,
    timeout: float = 20,
) -> None:
    deadline = time.monotonic() + timeout
    last_status = HTTPStatus.BAD_GATEWAY
    while time.monotonic() < deadline:
        last_status, _, _ = internal_http(
            "GET",
            url,
            token,
            timeout=2,
        )
        if last_status == HTTPStatus.OK:
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"internal service failed readiness; last status={last_status}"
    )


def checkpoint_workspace(workspace_id: str) -> dict[str, Any]:
    if not WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError("invalid workspace_id")
    sandbox_id = require_runtime_for(workspace_id, "checkpoint")
    # The archive is read into memory by internal_http and then spooled; both must
    # sit inside the queue slot or the gate bounds neither (object_queue_slot).
    with object_queue_slot():
        return _checkpoint_workspace_gated(workspace_id, sandbox_id)


def _checkpoint_workspace_gated(workspace_id: str, sandbox_id: str) -> dict[str, Any]:
    status, archive, content_type = internal_http(
        "GET",
        f"{runtime_endpoint(sandbox_id)}/v1/files/checkpoint",
        capability_ticket_for("workspace", workspace_id),
        timeout=120,
    )
    if status != HTTPStatus.OK:
        raise RuntimeError("workspace checkpoint creation failed")
    if content_type != "application/gzip":
        raise RuntimeError("workspace checkpoint returned an unexpected content type")
    if not archive or len(archive) > MAX_STREAM_OBJECT_BYTES:
        raise ValueError("workspace checkpoint exceeds object size limit")
    checkpoint_id = f"cp-{int(time.time())}-{secrets.token_hex(4)}"
    key = f"workspaces/{workspace_id}/checkpoints/{checkpoint_id}.tar.gz"
    digest = hashlib.sha256(archive).hexdigest()
    with tempfile.SpooledTemporaryFile(
        max_size=1024 * 1024,
        mode="w+b",
        dir="/tmp",
    ) as handle:
        handle.write(archive)
        handle.seek(0)
        object_put(
            OBJECT_STORE_WORKSPACE_BUCKET,
            key,
            handle,
            content_type="application/gzip",
            metadata={"Sha256": digest},
        )
    return {
        "checkpoint_id": checkpoint_id,
        "workspace_id": workspace_id,
        "bucket": OBJECT_STORE_WORKSPACE_BUCKET,
        "key": key,
        "bytes": len(archive),
        "sha256": digest,
        "content_type": "application/gzip",
    }


def restore_workspace_checkpoint(
    workspace_id: str,
    checkpoint_id: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    if not WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError("invalid workspace_id")
    checkpoint_id = validate_object_id(checkpoint_id, "checkpoint_id")
    key = f"workspaces/{workspace_id}/checkpoints/{checkpoint_id}.tar.gz"
    archive = object_get(
        OBJECT_STORE_WORKSPACE_BUCKET,
        key,
        MAX_STREAM_OBJECT_BYTES,
        expected_sha256=expected_sha256 or None,
    )
    digest = hashlib.sha256(archive).hexdigest()
    if expected_sha256 and not hmac.compare_digest(expected_sha256, digest):
        raise ValueError("workspace checkpoint sha256 does not match metadata")
    sandbox_id = require_runtime_for(workspace_id, "checkpoint restore")
    status, body, content_type = internal_http(
        "PUT",
        f"{runtime_endpoint(sandbox_id)}/v1/files/checkpoint",
        capability_ticket_for("workspace", workspace_id),
        body_bytes=archive,
        headers={
            "Content-Type": "application/gzip",
            "X-Content-SHA256": digest,
        },
        timeout=120,
    )
    if status != HTTPStatus.OK:
        try:
            detail = json.loads(body).get("error")
        except (json.JSONDecodeError, AttributeError):
            detail = None
        raise RuntimeError(detail or "workspace checkpoint restore failed")
    if "application/json" not in content_type:
        raise RuntimeError("workspace restore returned an unexpected content type")
    result = json.loads(body)
    return {
        "checkpoint_id": checkpoint_id,
        "workspace_id": workspace_id,
        "bucket": OBJECT_STORE_WORKSPACE_BUCKET,
        "key": key,
        "sha256": digest,
        **result,
    }


def _checkpoint_prefix(workspace_id: str) -> str:
    if not WORKSPACE_ID.fullmatch(workspace_id):
        raise ValueError("invalid workspace_id")
    return f"workspaces/{workspace_id}/checkpoints/"


def _parse_checkpoint_items(
    listed: list[dict[str, Any]], prefix: str
) -> list[dict[str, Any]]:
    checkpoints: list[dict[str, Any]] = []
    for item in listed:
        listed_key = str(item.get("key") or "")
        key = (
            listed_key
            if listed_key.startswith(prefix)
            else f"{prefix}{listed_key}"
        )
        if not key.startswith(prefix) or not key.endswith(".tar.gz"):
            continue
        checkpoint_id = key[len(prefix):-len(".tar.gz")]
        try:
            checkpoint_id = validate_object_id(
                checkpoint_id, "checkpoint_id"
            )
        except ValueError:
            continue
        checkpoints.append({
            "checkpoint_id": checkpoint_id,
            "key": key,
            "bytes": int(item.get("bytes") or 0),
            "last_modified": item.get("last_modified"),
        })
    return checkpoints


def list_workspace_checkpoints(workspace_id: str) -> dict[str, Any]:
    prefix = _checkpoint_prefix(workspace_id)
    listed = object_list(OBJECT_STORE_WORKSPACE_BUCKET, prefix)
    return {
        "workspace_id": workspace_id,
        "checkpoints": _parse_checkpoint_items(listed, prefix),
    }


def delete_workspace_checkpoint(
    workspace_id: str,
    checkpoint_id: str,
) -> dict[str, Any]:
    prefix = _checkpoint_prefix(workspace_id)
    checkpoint_id = validate_object_id(checkpoint_id, "checkpoint_id")
    key = f"{prefix}{checkpoint_id}.tar.gz"
    try:
        object_delete_versions(OBJECT_STORE_WORKSPACE_BUCKET, key)
    except RuntimeError:
        # S3-compatible stores are not required to implement object
        # versioning. Aliyun OSS in compatibility mode rejects versioned
        # operations with a bucket-ownership error while a plain delete
        # succeeds; without this fallback every workspace purge on such a
        # store fails and leaks the ownership quota row forever.
        object_delete(OBJECT_STORE_WORKSPACE_BUCKET, key)
    return {
        "workspace_id": workspace_id,
        "checkpoint_id": checkpoint_id,
        "deleted": True,
        "history_retained": False,
    }


def purge_workspace_checkpoints(workspace_id: str) -> int:
    result = list_workspace_checkpoints(workspace_id)
    checkpoints = result.get("checkpoints", [])
    for checkpoint in checkpoints:
        delete_workspace_checkpoint(
            workspace_id,
            str(checkpoint["checkpoint_id"]),
        )
    return len(checkpoints)






def runtime_exists(sandbox_id: str) -> RuntimeInstance | None:
    """Return a Runtime through the active Driver, or None when absent."""
    try:
        return configured_runtime_driver().get_runtime(sandbox_id)
    except RuntimeDriverError as exc:
        if exc.code == RuntimeDriverErrorCode.NOT_FOUND:
            return None
        raise


def _idle_runtime_victims(
    current: list[RuntimeInstance], idle_cutoff: float
) -> list[RuntimeInstance]:
    """When the pool is full, find the runtimes whose idleness exceeds the threshold - expires-at is reversed into the last touch.

    touch_runtime only advances expires-at when an MCP request/shell is active, so
    last_touch ≈ expires_at - SANDBOX_TTL_SECONDS; without activity expires-at is never
    advanced. An idle runtime is the message_user-hang / user-left scenario; evicting it
    only loses the running state (the workspace volume is kept), and the session's next tool call is
    transparently rebuilt by ensure_runtime."""
    victims = []
    for runtime in current:
        if runtime.expires_at is None:
            continue
        last_touch = runtime.expires_at - SANDBOX_TTL_SECONDS
        if last_touch < idle_cutoff:
            victims.append(runtime)
    return victims


def _admit_new_runtime(maximum: int) -> None:
    """Apply the global Runtime limit without exposing provider resources."""
    driver = configured_runtime_driver()
    current = driver.list_runtimes()
    if len(current) >= maximum:
        # LRU early eviction when the pool is full: sandboxes idle longer than
        # SANDBOX_IDLE_EVICT_SECONDS are released outright to free a slot for the new session -
        # the only victims are "idle" ones; active sandboxes are unaffected. List again after eviction to
        # confirm the slot is really free (eviction is an asynchronous delete; the list may not have converged yet).
        idle_cutoff = time.time() - SANDBOX_IDLE_EVICT_SECONDS
        for victim in _idle_runtime_victims(current, idle_cutoff):
            if not victim.runtime_id:
                continue
            print(
                f"[control_plane] evicting idle runtime {victim.runtime_id} "
                f"(idle >= {SANDBOX_IDLE_EVICT_SECONDS}s)",
                flush=True,
            )
            driver.delete_runtime(victim.runtime_id)
        current = driver.list_runtimes()
    if len(current) >= maximum:
        QUOTA_REJECTIONS.inc(gate="global")
        # Add transient semantics to the message. A model measured in session (glm) reads "capacity
        # reached" as a dead end, and right after thinking "deployment blocked" it
        # end_task(partial)s and gives up - while a concurrent session's sandbox is released a few minutes later.
        # The consumer (agent tool layer) can recognize the "transient" keyword and retry.
        raise KubeError(
            HTTPStatus.TOO_MANY_REQUESTS,
            f"runtime capacity reached ({len(current)}/{maximum}); "
            "transient — retry once other sandboxes are released",
        )


def touch_workspace(workspace_id: str) -> None:
    """Refresh the store's idle clock for a Workspace that was just used. Never fails the caller.

    Responsibility: the one entry point for "this Workspace is in use" on the control-plane side; the
         store throttles the write (Store.touch_workspace), so calling it on every request is cheap.
    🔴 Why it must be called from the Runtime life cycle and the data routes, not only from workspace
       admission: the reaper's idle verdict reads **only** sandbox_workspaces.last_used_at (never the
       volume marker, which the tenant can forge). Before this, only POST /v1/workspaces wrote it, so a
       client holding a lease for 6h+ without re-posting lost its Workspace the round its Runtime died.
    Constraint: a store failure here is logged and swallowed - the request already passed its gates and
         the idle window is hours; failing a file write because the touch did not land is the wrong
         trade. Silent is not acceptable either, so the skip leaves a line."""
    if STORE is None or not workspace_id:
        return
    try:
        STORE.touch_workspace(workspace_id)
    except StoreError as exc:
        print(f"warning: workspace touch skipped for {workspace_id}: {exc}", flush=True)


def touch_runtime(sandbox_id: str, now: int | None = None) -> RuntimeInstance:
    current = now or int(time.time())
    instance = configured_runtime_driver().touch_runtime(
        sandbox_id,
        current + SANDBOX_TTL_SECONDS,
    )
    # A Runtime kept alive is a Workspace in use; see touch_workspace for why the
    # store must hear about it and not only about workspace admission.
    touch_workspace(instance.workspace_id)
    return instance


def volume_agent_request(
    method: str,
    path: str,
    payload: dict | None = None,
    query: dict[str, str] | None = None,
    timeout: float = 40,
) -> tuple[int, bytes, str]:
    """Call the volume role. It is the only hand the Control Plane has on the Workspace volume."""
    if not VOLUME_AGENT_URL or not VOLUME_AGENT_TOKEN:
        raise WorkspaceOffline(
            "no volume agent configured; set VOLUME_AGENT_URL/VOLUME_AGENT_TOKEN"
        )
    return internal_http(
        method,
        f"{VOLUME_AGENT_URL}{path}",
        VOLUME_AGENT_TOKEN,
        payload,
        query,
        timeout=timeout,
    )


def ensure_workspace(workspace_id: str) -> dict:
    """Make sure the Workspace exists on the volume (idempotent).

    Responsibility: only ensure the directory structure is in place; **no Pod is created any more** - file-service has been folded into
         the Runtime, and a Workspace went from "a resident Pod" back to "a directory on the volume".
    Constraint: the Runtime's initContainer also creates the same set of directories; whichever comes first is fine.

    🔴 Constraint: the quota criterion changed from "number of Pods" to "number of directories", but **this gate cannot disappear because of that**.
         Now that a Workspace no longer counts against the Pod quota, without it nothing stops unlimited creation -
         the volume fills up with directories, silently. Admission is atomic in the volume role, which owns the shared directory inventory."""
    status, body, _ = volume_agent_request(
        "POST",
        f"/v1/workspaces/{workspace_id}",
        {},
        query={"maximum": str(MAX_WORKSPACES)},
    )
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        try:
            message = json.loads(body).get("error")
        except (json.JSONDecodeError, AttributeError):
            message = None
        raise KubeError(
            HTTPStatus.TOO_MANY_REQUESTS,
            message or f"workspace capacity reached ({MAX_WORKSPACES})",
        )
    if status != HTTPStatus.OK:
        raise RuntimeError(
            f"volume agent failed to create workspace: {body[:200]!r}"
        )
    # Create the idempotent per-workspace PVC after its directory exists.
    # Subdir provisioners map PVC names to directories at the export root,
    # converging to the same view as the volume role. Do not roll back the
    # directory on PVC failure: recreating an idempotent directory is cheap,
    # while a partial PVC state is harder to diagnose.
    # RWO development clusters cannot mount one dynamically-created PVC into
    # both the long-lived volume agent and a Runtime Pod. In shared mode the
    # pre-created WORKSPACE_PVC is mounted with a workspace-id subPath instead.
    if WORKSPACE_STORAGE_MODE == "per-workspace":
        KUBE.create_or_get(
            WORKLOAD_NAMESPACE,
            "persistentvolumeclaims",
            workspace_id,
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {
                    "name": workspace_id,
                    "namespace": WORKLOAD_NAMESPACE,
                    "labels": {
                        "app.kubernetes.io/managed-by": "sandbox-controller",
                        "convee.io/workspace-id": workspace_id,
                    },
                },
                "spec": {
                    "accessModes": ["ReadWriteMany"],
                    "storageClassName": os.getenv(
                        "SANDBOX_RWX_STORAGE_CLASS", "sandbox-rwx"
                    ),
                    "resources": {"requests": {"storage": WORKSPACE_QUOTA}},
                },
            },
        )
    return json.loads(body)


def require_runtime_for(workspace_id: str, operation: str) -> str:
    """Operations that need an execution environment fetch the Runtime here; raise when there is none.

    checkpoint / restore fall into this category: they pack or unpack the whole Workspace, and running that on the
    shared volume replica means a large Workspace can crowd out every tenant's file access. Same
    discipline as glob/grep."""
    sandbox_id = runtime_serving_workspace(workspace_id)
    if not sandbox_id:
        raise WorkspaceOffline(
            f"{operation} requires a running Runtime for {workspace_id}"
        )
    return sandbox_id


# --- Runtime state records --------------------------------------------------
# 🔴 **Recording a row's state** and **holding a tenant quota slot** are two different things. They were originally tied to the same line
# in STORE.admit_runtime: the row was written only when "there is a store + a tenant + a quota". So
# single-tenant deployments (SANDBOX_STORE_BACKEND not configured) and the management plane's global identity (admin key without
# X-Sandbox-Tenant) provisioned runtimes without any row, and when
# provisioning failed the status was unknown all along - the "get the ID immediately, then poll for the terminal state" contract did not
# hold at all for those identities, and the client could only guess by timeout.
# Now: the row is always written; the quota is taken only when a tenant quota applies.

#: Without a store, state can only be recorded in-process. The cap keeps it from growing without bound on a long-running Control Plane
#: - this table only serves the short stretch "provisioning in progress → terminal state", and 512 entries cover any burst of concurrent provisionings.
#: ⚠️ Boundary: lost on process restart (back to the old "unknown"), and not shared across replicas. A deployment without a database
#: has no shared control-plane state at all (quota, tenants, and audit are all off). This fallback matches
#: the assumptions of that deployment form; terminal states visible across replicas require configuring the store.
MAX_MEMORY_RUNTIME_STATES = 512
_MEMORY_RUNTIME_STATES: dict[str, dict] = {}
_MEMORY_RUNTIME_LOCK = threading.Lock()


def runtime_state_key(
    tenant_id: str | None, tenant_max_runtimes: int | None
) -> str:
    """Which key this provisioning's state record is filed under.

    With a tenant and an allocated quota it is filed under the tenant's name - that row is also the quota slot it holds;
    otherwise it goes under the sentinel key, recording state only and never counting against any tenant's quota.

    🔴 Must be a pure function: reservation (request thread, before the 202 is returned) and closing (activate/release,
       background thread) are two independent calls. If the keys computed on the two sides differ, they never hit the same row -
       which shows up as a provisioned runtime stuck in pending forever."""
    if tenant_id and tenant_max_runtimes is not None:
        return tenant_id
    return UNTENANTED_RUNTIME


def _remember_runtime_state(
    sandbox_id: str, workspace_id: str, template_id: str
) -> None:
    with _MEMORY_RUNTIME_LOCK:
        while len(_MEMORY_RUNTIME_STATES) >= MAX_MEMORY_RUNTIME_STATES:
            _MEMORY_RUNTIME_STATES.pop(next(iter(_MEMORY_RUNTIME_STATES)))
        _MEMORY_RUNTIME_STATES[sandbox_id] = {
            "tenant": UNTENANTED_RUNTIME,
            "sandbox_id": sandbox_id,
            "workspace_id": workspace_id,
            "template": template_id,
            "status": "pending",
        }


def _transition_memory_state(
    sandbox_id: str, target: str, expected: tuple[str, ...]
) -> bool:
    """Same rule as Store._transition: conditional update; a terminal state may not be overwritten unconditionally."""
    with _MEMORY_RUNTIME_LOCK:
        record = _MEMORY_RUNTIME_STATES.get(sandbox_id)
        if record is None or record["status"] not in expected:
            return False
        record["status"] = target
        return True


def reserve_runtime_state(
    sandbox_id: str,
    workspace_id: str,
    template_id: str,
    tenant_id: str | None,
    tenant_max_runtimes: int | None,
) -> None:
    """Write the pending record for this provisioning; identities that hold a quota take the slot at the same time.

    Constraint: the quota-taking path still has **exactly one entry point, STORE.admit_runtime** - its
         `INSERT ... WHERE count < limit` is the only gate for the per-tenant quota, and nothing here
         branches around it. Identities without a quota go through record_untenanted_runtime, which
         writes the sentinel tenant and never charges a real tenant."""
    if STORE is None:
        _remember_runtime_state(sandbox_id, workspace_id, template_id)
        return
    # 🔴 A new Runtime for this Workspace means a new Workspace capability key.
    # Rotating here rather than never is what makes "restart rotates" true for
    # the Workspace credential: the Pod that held the previous one is being
    # replaced, and anything that kept a copy of it stops being able to use it.
    # Must run before the manifest is built, which is why it sits at the start
    # of reservation rather than next to activation.
    STORE.bump_workspace_epoch(workspace_id)
    if runtime_state_key(tenant_id, tenant_max_runtimes) == UNTENANTED_RUNTIME:
        STORE.record_untenanted_runtime(sandbox_id, workspace_id, template_id)
        return
    # The per-tenant quota relies on the store's atomic admission, not on counting Pod labels. Counting labels either
    # runs inside an in-process lock (fails under multiple replicas) or outside it (over-issues under concurrency); the store's INSERT ... WHERE
    # count < limit has neither problem.
    if not STORE.admit_runtime(
        tenant_id,
        sandbox_id,
        workspace_id,
        template_id,
        tenant_max_runtimes,
    ):
        QUOTA_REJECTIONS.inc(gate="tenant")
        raise KubeError(
            HTTPStatus.TOO_MANY_REQUESTS,
            f"tenant {tenant_id} runtime capacity reached "
            f"({tenant_max_runtimes}/{tenant_max_runtimes})",
        )


def activate_runtime_state(state_key: str, sandbox_id: str) -> bool:
    """pending → active. False = this row is no longer pending (released concurrently)."""
    if STORE is None:
        return _transition_memory_state(sandbox_id, "active", ("pending",))
    return STORE.activate_runtime(state_key, sandbox_id)


def release_runtime_state(
    state_key: str, sandbox_id: str, *, failed: bool = False
) -> bool:
    """→ released/failed. False = already in a terminal state (duplicate release)."""
    if STORE is None:
        return _transition_memory_state(
            sandbox_id,
            "failed" if failed else "released",
            ("pending", "active"),
        )
    return STORE.release_runtime(state_key, sandbox_id, failed=failed)


def read_runtime_state(sandbox_id: str) -> dict | None:
    """Read the state record (terminal states included) by id. The client of an asynchronous provisioning relies on it to learn whether provisioning finished."""
    if STORE is None:
        with _MEMORY_RUNTIME_LOCK:
            record = _MEMORY_RUNTIME_STATES.get(sandbox_id)
            return dict(record) if record else None
    return STORE.get_runtime(sandbox_id)


def ensure_runtime(
    sandbox_id: str,
    workspace_id: str,
    template_id: str = "default",
    tenant_id: str | None = None,
    tenant_max_runtimes: int | None = None,
    *,
    reserved: bool = False,
) -> dict:
    """Make sure the Runtime exists (idempotent).

    Constraint: the global-layer quota is decided inside the admission lock and protects the node from filling up; the per-tenant layer keeps
         tenants from crowding each other out - with only a global quota, one tenant could legitimately take every slot.
         `tenant_max_runtimes=None` means no limit, matching single-tenant behavior.

    reserved=True means the caller (asynchronous path) already wrote the pending record before returning 202
    and the quota is taken; only the K8s part is done here. Closing (activate) and rollback (release) still
    happen here - the two paths share the same stretch of the state machine and do not grow into two copies.

    🔴 Why the per-tenant layer may sit outside the lock (asynchronous path): its atomicity comes from the store's
       `INSERT ... WHERE count < limit` and never relied on this in-process lock - which does not hold across
       multiple Control Plane replicas anyway. Each layer guards its own resource: take the tenant slot first, and when the global gate
       then rejects, the except rollback returns it. The failure direction is conservative."""
    driver = configured_runtime_driver()
    runtime_spec = RuntimeSpec(
        runtime_id=sandbox_id,
        workspace_id=workspace_id,
        template_id=template_id,
        tenant_id=tenant_id,
    )
    created = False
    # admitted = "there is a pending record here waiting to be closed", which is not the same as "took a tenant quota slot":
    # identities that hold no quota also have a record to activate/release.
    admitted = reserved
    state_key = runtime_state_key(tenant_id, tenant_max_runtimes)
    started_at = time.monotonic()
    # 🔴 The admission section must also be inside the try. The quota is taken before the Pod is created, and creating the Pod is exactly
    # the step most likely to fail - with the with block outside the try, create_or_get raising would jump
    # out before created is set, the quota would leak permanently, and all that shows on site is one failed provisioning.
    try:
        with _RUNTIME_ADMISSION_LOCK:
            # Workspace is the natural idempotency key for runtime creation.
            # A client may time out after Kubernetes committed the Pod but
            # before the HTTP 201 arrives; a retry must reuse that Pod instead
            # of consuming another runtime slot.
            existing = driver.list_for_workspace(workspace_id)
            for candidate in existing:
                if not candidate.provider_id or not candidate.runtime_id:
                    continue
                if not candidate.ready:
                    candidate = wait_for_runtime(candidate.runtime_id)
                touch_runtime(candidate.runtime_id)
                return candidate
            pod = runtime_exists(sandbox_id)
            if pod is None:
                _admit_new_runtime(MAX_RUNTIMES)
                if not admitted:
                    # The synchronous path reserves here: sandbox_id was generated by this request, and nobody
                    # can hold it for polling before this point, so the row is written inside the lock after confirming the Pod does
                    # not exist - that way repeated calls with the same sandbox_id (idempotent semantics) do not take the same
                    # quota twice. The asynchronous path is the reverse: the row must precede the 202, see
                    # spawn_runtime_creation.
                    reserve_runtime_state(
                        sandbox_id,
                        workspace_id,
                        template_id,
                        tenant_id,
                        tenant_max_runtimes,
                    )
                    admitted = True
                with runtime_create_phase("kubernetes_create"):
                    driver.create_runtime(runtime_spec)
                created = True
        with runtime_create_phase("endpoint_ready"):
            driver.ensure_endpoint(sandbox_id)
        with runtime_create_phase("pod_ready"):
            pod = wait_for_runtime(sandbox_id)
        try:
            health_ticket = capability_ticket_for("runtime", sandbox_id)
        except ValueError as exc:
            #The row is gone, which at this point can only mean a concurrent
            #release terminated this sandbox while its Pod was coming up. That
            #is the same situation the activate_runtime_state check below
            #reports, and it deserves the same answer: without this the epoch
            #lookup raises first and the caller gets an opaque failure instead
            #of "released while starting up", losing the one sentence that
            #explains what happened.
            raise KubeError(
                HTTPStatus.CONFLICT,
                f"runtime {sandbox_id} was released while starting up",
            ) from exc
        with runtime_create_phase("runtime_health"):
            wait_for_internal_health(
                f"{driver.endpoint(sandbox_id)}/healthz",
                health_ticket,
            )
        if admitted and not activate_runtime_state(state_key, sandbox_id):
            # Conditional-update miss = this record is no longer pending, which can only mean a concurrent
            # release terminated it (the sandbox was deleted midway). The Pod is alive right now but the record
            # is gone; what remains is an unowned sandbox holding no quota - tear it down and report the error truthfully.
            # Do not count it as a success just because the Pod happens to be up.
            raise KubeError(
                HTTPStatus.CONFLICT,
                f"runtime {sandbox_id} was released while starting up",
            )
        RUNTIME_CREATE_SECONDS.observe(time.monotonic() - started_at)
        # The reuse branch above touches through touch_runtime; a fresh Runtime is
        # the same signal for the Workspace's idle clock.
        touch_workspace(workspace_id)
        return pod
    except Exception as exc:
        RUNTIME_CREATE_FAILURES.inc(reason=create_failure_reason(exc))
        if created:
            # delete_runtime includes the step that returns the quota, so do not release it again here.
            delete_runtime(sandbox_id)
        elif admitted:
            # The record was written but no Pod was created: create_or_get itself failed, or the global gate
            # rejected after the reservation. delete_runtime has nothing to do; the terminal state (and the quota) must be settled here.
            with contextlib.suppress(StoreError):
                release_runtime_state(state_key, sandbox_id, failed=True)
        raise


def spawn_runtime_creation(
    sandbox_id: str,
    workspace_id: str,
    template_id: str,
    tenant_id: str | None,
    tenant_max_runtimes: int | None,
) -> None:
    """Provision the Runtime in the background and return immediately.

    Responsibility: concurrency cap + **write the pending record before returning 202**, then start the thread;
         rollback and state transitions stay in ensure_runtime, so the synchronous and asynchronous paths share the same stretch of
         the state machine and do not grow into two copies.
    Constraint: the caller must obtain the sandbox_id first and return it to the client - the client polls with it.

    🔴 Why the reservation must be in this function (request thread) and cannot be left to the background thread:
       as soon as the caller gets the 202 it GETs /v1/sandboxes/{id}, while the background thread first has to grab the admission lock
       and make another 3 K8s round trips before it is its turn to insert the row - the lock is held across all 3 round trips, and in a burst of
       concurrent provisionings the last one queues behind roughly 21 round trips; the window is **seconds**. That GET inside the window
       hits require_sandbox_tenant: the store has no row and the Pod does not exist yet, so
         ① it answers 404 - exactly the 404 the asynchronous contract is meant to avoid (cannot tell "never provisioned" from "provisioned but
            failed");
         ② it writes an audit row with outcome="denied" - that signal is meant for "someone probing with IDs", the
            attack precursor (described in the table comment in store.py); contaminated by normal provisioning, its signal-to-noise ratio is ruined.
       With the row written before the 202, both problems vanish at once: the client holding the id means the row is already there.

    🔴 No retry on failure. Provisioning a Pod is not idempotent (it takes a quota slot and creates a Pod); an automatic retry would leave
       duplicates behind on both the quota and the Pod. ensure_runtime's rollback has already returned the quota, and the client, once it sees
       the terminal state, decides for itself whether to try again."""
    if not _CREATE_SLOTS.acquire(blocking=False):
        raise KubeError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            f"too many creations in flight ({MAX_INFLIGHT_CREATES})",
        )
    try:
        reserve_runtime_state(
            sandbox_id,
            workspace_id,
            template_id,
            tenant_id,
            tenant_max_runtimes,
        )
    except BaseException:
        # A failed reservation (quota full → 429, store unavailable → 503) returns the concurrency slot right here, otherwise every
        # failure leaks one slot and after a few of them nothing can be provisioned any more. The exception propagates as usual: these rejections happen
        # in the request thread, the caller learns of them on the spot and does not need to get a 202 first and then poll for failed.
        _CREATE_SLOTS.release()
        raise

    parent_trace_id = tracing.current_trace_id()
    parent_flags = tracing.current_flags()
    parent_span_id = tracing.current_span_id()

    def _run() -> None:
        global _CREATE_INFLIGHT
        if parent_trace_id:
            tracing.set_current(parent_trace_id, parent_flags, parent_span_id)
        with _CREATE_INFLIGHT_LOCK:
            _CREATE_INFLIGHT += 1
        try:
            ensure_runtime(
                sandbox_id,
                workspace_id,
                template_id,
                tenant_id,
                tenant_max_runtimes,
                reserved=True,
            )
        except Exception as exc:
            # The state has already been finalized by ensure_runtime's rollback and is visible to the polling client.
            # Only a log line is left here - not storing the reason in the store is deliberate: that would require adding an error column
            # to sandbox_runtimes, and the error text is uncontrolled external input, so length, sensitive
            # content, and query surface would all have to be considered before persisting it. For now the reason lives in the log and in the metric's reason label.
            print(
                f"[create] {sandbox_id} failed: {exc}",
                flush=True,
            )
        finally:
            with _CREATE_INFLIGHT_LOCK:
                _CREATE_INFLIGHT -= 1
                # Wake the shutdown orchestration waiting for this batch of threads to finish, see await_pending_creations.
                _CREATE_INFLIGHT_LOCK.notify_all()
            _CREATE_SLOTS.release()

    thread = threading.Thread(
        target=_run, name=f"create-{sandbox_id}", daemon=True
    )
    try:
        thread.start()
    except BaseException as exc:
        # 🔴 Same discipline as the reservation-failure section above; the only difference is that this one follows the **thread start** step.
        # The concurrency slot and the pending row were both taken outside _run, and their cleanup code sits in _run's
        # finally - code that never executes when the thread never started at all:
        #   · Concurrency slot: one lost permanently per failure; after MAX_INFLIGHT_CREATES of them,
        #     asynchronous provisioning returns 503 until the process restarts, but all that shows on site is "capacity full", not that
        #     the capacity was eaten by a leak (there is not a single running Pod in the cluster).
        #   · Pending row: it holds a tenant quota slot until the PENDING_STALE_SECONDS (10 minutes by
        #     default) cleanup reclaims it, yet the client never even received the sandbox_id.
        # Thread.start() raises RuntimeError when the process cannot support a new thread - the same thing the mc
        # queue gate guards against (no cap on the number of threads), just hit from the other end.
        with contextlib.suppress(StoreError):
            release_runtime_state(
                runtime_state_key(tenant_id, tenant_max_runtimes),
                sandbox_id,
                failed=True,
            )
        _CREATE_SLOTS.release()
        # Convert to 503 instead of letting the RuntimeError fall through to the end of do_POST - that would return 400,
        # and the caller would change its request, when this is "cannot start a thread right now" and simply retrying later is right. Same
        # thing returned as when the concurrency slots are full, above.
        raise KubeError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "cannot start a background creation right now",
        ) from exc


def delete_runtime(sandbox_id: str) -> None:
    """Delete the Runtime and return the slot it held.

    🔴 The order cannot be reversed: remove the endpoint and compute before
       returning quota. The other way round, a failed deletion leaves "quota
       returned but sandbox still running", which is over-issuing; this order
       fails conservatively and reconciliation can repair the held slot."""
    if STORE is not None:
        # Revoke first: from this point Control Plane can mint nothing the running Pod
        # would accept, so the credential is dead even if the deletion below
        # fails and the Pod lingers. Tickets already issued still run out their
        # own expiry - that window is the ticket TTL and nothing longer.
        with contextlib.suppress(StoreError):
            STORE.bump_runtime_epoch(sandbox_id)
    configured_runtime_driver().delete_runtime(sandbox_id)
    if STORE is None:
        # Without a store the state is recorded in-process and must be terminated here too - otherwise the Pod is gone but
        # GET /v1/sandboxes/{id} still reports active.
        release_runtime_state(UNTENANTED_RUNTIME, sandbox_id)
        return
    workspace_id = ""
    try:
        record = STORE.get_runtime(sandbox_id)
        workspace_id = str((record or {}).get("workspace_id") or "")
        owner = STORE.runtime_owner(sandbox_id)
        if owner:
            STORE.release_runtime(owner, sandbox_id)
    except StoreError as exc:
        # Same handling as remove_workspace_data: the data is gone but the bookkeeping did not catch up.
        # The quota stays taken until reconcile or the next DELETE retry.
        print(
            f"warning: runtime {sandbox_id} deleted but its quota slot "
            f"survives until reconciled: {exc}",
            flush=True,
        )
    # 🔴 The Runtime dying is the moment the Workspace's idle clock should **start**, not the moment it
    # should be found already expired: the reaper sweeps idle Workspaces in the same round it deletes
    # expired Runtimes, and a Workspace whose only activity went through its Runtime has a stale column.
    # Read from the store row rather than the Pod: the Pod is already gone at this point.
    touch_workspace(workspace_id)


def remove_workspace_data(workspace_id: str) -> dict[str, Any]:
    """Delete the whole Workspace. See the volume role for how it differs from purge (which only purges content and leaves .sandbox).

    🔴 When the directory is absent on the volume (the volume role returns 404), it counts as **deleted** rather than as a failure.
    DELETE must be idempotent, and there is a concrete bad consequence here: the ownership row is cleared after the data is deleted.
    Once the directory is gone but the row remains (the first cleanup failed, or the data was collected by GC),
    a retry would get stuck at 404 -> RuntimeError -> 400 and never reach the step that clears the row.
    The tenant's quota would then never be returned - only a manual database edit could fix it."""
    status, body, content_type = volume_agent_request(
        "DELETE",
        f"/v1/workspaces/{workspace_id}",
        query={"remove": "1"},
        timeout=120,
    )
    if status == HTTPStatus.NOT_FOUND:
        return {"workspace_id": workspace_id, "removed": False, "absent": True}
    if status != HTTPStatus.OK or "application/json" not in content_type:
        raise RuntimeError("workspace removal failed")
    # The per-workspace PVC shares the directory's life cycle. With archiveOnDelete=false on the provisioner,
    # deleting the PVC deletes the directory under the export root; the volume DELETE above has already cleared the directory.
    # What is deleted here is the PVC and the storage claim under its name. 404 is idempotent (KUBE.delete does not raise on 404);
    # the old form (shared-PVC era) has no PVC of that name and passes silently.
    with contextlib.suppress(KubeError):
        KUBE.delete(
            WORKLOAD_NAMESPACE, "persistentvolumeclaims", workspace_id
        )
    return json.loads(body)


def sandbox_view(runtime: RuntimeInstance) -> dict:
    return {
        "id": runtime.runtime_id,
        "workspace_id": runtime.workspace_id,
        "status": runtime.state,
        "runtime_class": runtime.isolation,
        # Read back the template actually in effect: the caller confirms it is not the default image. Do not return the image
        # itself - that would leak the internal registry topology to callers holding Control Plane tokens.
        # Historical Pods have no such label and are presented as default.
        "template": runtime.template_id,
        "created_at": runtime.created_at,
        "expires_at": (
            str(runtime.expires_at) if runtime.expires_at is not None else None
        ),
    }


_BINARY_QUANTITY_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
_DECIMAL_QUANTITY_UNITS = {
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


def cpu_millicores(value: object) -> int | None:
    """Normalize a Kubernetes CPU quantity to integer millicores."""
    if not isinstance(value, str) or not value:
        return None
    units = {"n": Decimal("0.000001"), "u": Decimal("0.001"), "m": Decimal(1)}
    suffix = value[-1] if value[-1] in units else ""
    raw = value[:-1] if suffix else value
    try:
        amount = Decimal(raw) * (units[suffix] if suffix else Decimal(1000))
    except InvalidOperation:
        return None
    return max(0, int(amount))


def memory_bytes(value: object) -> int | None:
    """Normalize a Kubernetes memory/storage quantity to integer bytes."""
    if not isinstance(value, str) or not value:
        return None
    multiplier = 1
    raw = value
    for suffix, factor in {**_BINARY_QUANTITY_UNITS, **_DECIMAL_QUANTITY_UNITS}.items():
        if value.endswith(suffix):
            raw = value[: -len(suffix)]
            multiplier = factor
            break
    try:
        amount = Decimal(raw) * multiplier
    except InvalidOperation:
        return None
    return max(0, int(amount))


def runtime_monitoring_view(
    runtime: RuntimeInstance,
    usage: RuntimeUsage | None = None,
) -> dict:
    """Return a registry-safe, normalized Runtime resource snapshot."""
    view = sandbox_view(runtime)
    view.update(
        {
            "node": runtime.node,
            "ready": runtime.ready,
            "restarts": runtime.restarts,
            "cpu": {
                "usage_millicores": (
                    usage.cpu_usage_millicores if usage else None
                ),
                "request_millicores": runtime.cpu_request_millicores,
                "limit_millicores": runtime.cpu_limit_millicores,
            },
            "memory": {
                "usage_bytes": usage.memory_usage_bytes if usage else None,
                "request_bytes": runtime.memory_request_bytes,
                "limit_bytes": runtime.memory_limit_bytes,
            },
        }
    )
    return view


def node_monitoring_view(node: dict, metric: dict | None = None) -> dict:
    """Return node health/capacity without machine IDs or raw Kube payloads."""
    metadata = node.get("metadata", {})
    status = node.get("status", {})
    ready = next(
        (item for item in status.get("conditions", []) if item.get("type") == "Ready"),
        {},
    )
    labels = metadata.get("labels", {})
    roles = sorted(
        key.removeprefix("node-role.kubernetes.io/")
        for key in labels
        if key.startswith("node-role.kubernetes.io/")
    ) or ["worker"]
    capacity = status.get("capacity", {})
    allocatable = status.get("allocatable", {})
    usage = metric.get("usage", {}) if metric else {}
    node_info = status.get("nodeInfo", {})
    return {
        "name": metadata.get("name"),
        "status": "ready" if ready.get("status") == "True" else "not_ready",
        "roles": roles,
        "unschedulable": bool(node.get("spec", {}).get("unschedulable")),
        "cpu": {
            "usage_millicores": cpu_millicores(usage.get("cpu")),
            "allocatable_millicores": cpu_millicores(allocatable.get("cpu")),
            "capacity_millicores": cpu_millicores(capacity.get("cpu")),
        },
        "memory": {
            "usage_bytes": memory_bytes(usage.get("memory")),
            "allocatable_bytes": memory_bytes(allocatable.get("memory")),
            "capacity_bytes": memory_bytes(capacity.get("memory")),
        },
        "pod_capacity": int(capacity.get("pods", 0)),
        "kubelet_version": node_info.get("kubeletVersion"),
        "os_image": node_info.get("osImage"),
        "architecture": node_info.get("architecture"),
    }


def workspace_view(
    entry: dict,
    runtime_attached: bool,
    *,
    tenant_id: str | None = None,
    recorded_last_used_at: str | None = None,
) -> dict:
    """Interface model: read-only view of a Workspace.

    Responsibility: let operators see whether a Workspace is about to be reclaimed; not responsible for listing the files inside
         (that needs the files interface; this view does not touch the data plane).

    The data source moved from the Pod annotation to the .sandbox marker on the volume - a Workspace no longer has its own
    Pod. **No field changed**: what callers see (the Agent's sandbox_client, the Console)
    is structurally identical to before.

    status is always "ready": this field used to describe the readiness of the file-service Pod,
    which has since been folded into the Runtime. The directory is available now and there is no second state to speak of - the field is kept
    to avoid breaking the contract, not because it still carries information.

    AI-LOCK: idle_expires_at and runtime_attached must be read together. The reclaim criterion is
         `reap_once`'s "**no active Runtime** and last_used_at + IDLE_TTL
         has passed" - looking at the time alone leads to the wrong conclusion "should have been reclaimed long ago but is still there", which is
         the normal situation while a Runtime is attached. The two fields are presented separately to keep people from reading only one.

    🔴 Two clocks, and only one of them decides. `last_used_at` in the view is the volume marker (file
       activity inside the sandbox refreshes it; the tenant can also forge it). The reaper's verdict reads
       the store column (idle_workspaces), which touch_workspace refreshes. `recorded_last_used_at` is
       that column; when the caller supplies it, `idle_expires_at` is derived from it so the countdown
       shown is the countdown the reaper runs. Without it (no store, legacy rows) the marker is the only
       clock there is and the old derivation stands. The Workspace schema forbids extra fields, so the
       store value is not exposed as a field of its own."""
    last_used_at = entry.get("last_used_at") or entry.get("created_at")
    idle_expires_at = None
    reclaim_clock = recorded_last_used_at or last_used_at
    if not runtime_attached and reclaim_clock:
        try:
            idle_expires_at = str(
                int(reclaim_clock) + WORKSPACE_IDLE_TTL_SECONDS
            )
        except (TypeError, ValueError):
            idle_expires_at = None
    view = {
        "id": entry.get("id"),
        "status": "ready",
        "created_at": entry.get("created_at"),
        "last_used_at": last_used_at,
        "runtime_attached": runtime_attached,
        "idle_expires_at": idle_expires_at,
    }
    # Only the management-plane view carries ownership: when a tenant looks at its own list this field always equals itself,
    # and there is no point in an extra column that says nothing.
    if tenant_id is not None:
        view["tenant"] = tenant_id
    return view


def runtime_serving_workspace(workspace_id: str) -> str | None:
    """The Runtime attached to this Workspace whose Runtime MCP is ready.

    The criterion is **Pod Ready** rather than "Pod exists": for a few seconds after creation the Runtime's serving
    port is not listening yet, and forwarding would only get a connection refused."""
    runtimes = configured_runtime_driver().list_for_workspace(workspace_id)
    for runtime in runtimes:
        if not runtime.ready:
            continue
        if runtime.runtime_id:
            return runtime.runtime_id
    return None


def attached_workspace_ids(now: int | None = None) -> set[str]:
    """The set of Workspaces that currently have a Runtime attached.

    AI-LOCK: the criterion must match `reap_once`'s active_workspaces item by item, including
        "an expired Runtime does not count as attached" - it is deleted in the next round (≤15s) and
        the Workspace then enters idle timing. Counting it as attached, operators would see
        "RUNTIME=yes / IDLE=-" and then wait for a reclaim countdown that is in fact about to start.
        Observation drifting from the reclaim criterion is worse than no observation. Changing this requires changing reap_once at the same time."""
    current = now or int(time.time())
    runtimes = configured_runtime_driver().list_runtimes()
    attached: set[str] = set()
    for runtime in runtimes:
        if (
            runtime.runtime_id
            and runtime.expires_at
            and runtime.expires_at <= current
        ):
            continue
        if runtime.workspace_id:
            attached.add(runtime.workspace_id)
    return attached


def probe_runtime_busy(sandbox_id: str) -> bool:
    """Core function: after the TTL expires, ask the Runtime whether it is still working.

    Responsibility: answer "should it be spared?" only; not responsible for renewal or deletion.

    Any exception returns False (eligible for deletion). Failing open would let
    crashed or unreachable Runtimes occupy capacity indefinitely.

    Constraint: the timeout must be short (the sandbox's internal status query should not be slow), and it is only called for
        **expired** Runtimes, so the cost is zero otherwise."""
    request = Request(
        f"{runtime_endpoint(sandbox_id)}/activity",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {capability_ticket_for('runtime', sandbox_id)}",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=ACTIVITY_PROBE_TIMEOUT) as response:
            payload = json.loads(response.read())
    except (HTTPError, OSError, TimeoutError, URLError, ValueError):
        return False
    return bool(payload.get("busy"))


def forget_workspace_row(workspace_id: str, owner: str | None = None) -> None:
    """Core function: once the Workspace data is gone, return the quota slot it held as well.

    Responsibility: clear the ownership row in the store only; not responsible for deleting the data on the volume (the caller has already done that).
    Constraint: **every path that reclaims a Workspace must call this**. There are two at the moment - a user-initiated
         DELETE and the reaper's idle reclaim. Doing it on only one is worse than a missed bookkeeping entry: the ghost row
         holds the per-tenant quota slot forever, and scope_workspaces intersects the volume entries
         with the ownership table, so a row without an entry cannot be listed by the tenant at all ⇒ cannot be seen or deleted.
         The quota cannot be recovered, only raised.
         That is also why this was extracted from forget_workspace_ownership: one implementation with two
         call sites is harder to miss than two implementations.
    When owner is None, look it up from the store - self.tenant_id is None when the management identity deletes someone
    else's Workspace, and using it as the owner would clear nothing."""
    if STORE is None:
        return
    try:
        resolved = owner or STORE.owner_of(workspace_id)
        if resolved is None:
            # No row: pre-multi-tenancy stock.
            return
        STORE.forget_workspace(resolved, workspace_id)
    except StoreError as exc:
        print(
            f"warning: workspace {workspace_id} data removed but its "
            f"ownership row survives, the tenant quota slot stays taken "
            f"until this is retried: {exc}",
            flush=True,
        )




#: Set after receiving SIGTERM/SIGINT. The whole shutdown orchestration revolves around it.
_SHUTTING_DOWN = threading.Event()
# Grace period for being removed from Endpoints. K8s removing a Pod from Endpoints is not instantaneous: the kubelet delivering
# SIGTERM and the endpoint controller updating happen in parallel, and new requests keep arriving during the seconds in between.
# In that period /healthz already reports 503 (the readinessProbe fails because of it) but the service keeps accepting requests -
# drain traffic first, then stop the service. The other way round, every rolling update drops a batch of requests.
#
# 🔴 Must be well below the Deployment's terminationGracePeriodSeconds, otherwise SIGKILL
# arrives before the orchestration finishes and graceful shutdown becomes decoration. See SHUTDOWN_BUDGET_SECONDS for the total budget.
SHUTDOWN_DRAIN_SECONDS = float(os.getenv("SANDBOX_CONTROL_PLANE_SHUTDOWN_DRAIN_SECONDS", "5"))
# **Upper limit** on waiting for in-flight work to finish, not the time it takes: await_inflight / await_pending_creations
# both return the moment the count reaches zero. With nothing in flight this step costs nothing.
#
# 🔴 The lower bound is set by synchronous provisioning. POST /v1/sandboxes defaults to wait=true, and waits at most
# wait_for_runtime(90s) + internal health check(20s) = 110s. Anything smaller means shutdown cuts it off
# after admit_runtime and before activate_runtime, leaving three broken pieces:
# a pending row in the store (quota held), a running Pod in the cluster, and a client whose connection was reset
# and that never got the sandbox_id ⇒ it cannot even "delete and retry", only wait
# for the PENDING_STALE_SECONDS (10min) cleanup.
SHUTDOWN_INFLIGHT_SECONDS = float(
    os.getenv("SANDBOX_CONTROL_PLANE_SHUTDOWN_INFLIGHT_SECONDS", "120")
)
# Wait for the reaper to finish its current round. Also an upper limit: most of the time it is sitting in
# _SHUTTING_DOWN.wait(15), and returns as soon as the event is set.
#
# 🔴 Deliberately does **not** cover the worst case: one round's checkpoint GC walks the whole bucket one
# page at a time (object_list_page), each page a gated call with `read_timeout=60` per socket read, so a
# round has no overall ceiling - a slow store and a large bucket can take many minutes. Holding the Pod for
# that is not worth it - checkpoint / ticket GC is fully idempotent and continues next
# time; a delete_runtime cut off halfway (Pod deleted but quota not yet returned) is covered by the two-way reconciliation.
# This value governs a regular round: activity probes of 2s×N plus a few K8s round trips.
SHUTDOWN_REAPER_SECONDS = float(
    os.getenv("SANDBOX_CONTROL_PLANE_SHUTDOWN_REAPER_SECONDS", "60")
)
#: Total budget of the shutdown orchestration = drain + wait for in-flight + wait for reaper.
# 🔴 terminationGracePeriodSeconds must exceed it, otherwise SIGKILL interrupts the orchestration, and
# the failure mode is "occasionally a few dropped requests / occasionally an extra pending row", hard to attribute to this.
# test_grace_period_covers_the_whole_orchestration pins this.
SHUTDOWN_BUDGET_SECONDS = (
    SHUTDOWN_DRAIN_SECONDS
    + SHUTDOWN_INFLIGHT_SECONDS
    + SHUTDOWN_REAPER_SECONDS
)


class GracefulHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer plus the one thing the base class does not do: actually waiting for in-flight requests to finish.

    🔴 The base class cannot wait for anyone, and it fails quietly:
       1. The join only happens in server_close(), which the Control Plane never called before;
       2. Even when called it does not wait - the first line of socketserver._Threads.append is
          `if thread.daemon: return`, and http.server sets daemon_threads to
          True, so no request thread ever enters the join list. server_close() returns as usual,
          having waited for nothing.
       The sentence "ThreadingHTTPServer waits for in-flight requests" is always false for this process.

    Constraint: daemon_threads staying True is intentional. The wait must have an upper limit; after the timeout the process still
         has to get out of the way - a stuck request dragging the Pod to SIGKILL is worse than giving up on it.
         So the count is kept here instead of making request threads non-daemon and handing the join to the base class."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # The counters must exist before super().__init__: it binds+listens, and from then on
        # a connection may arrive at any time, and process_request_thread uses these two fields as soon as it comes up.
        self._inflight = 0
        self._inflight_done = threading.Condition()
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        # Counted here rather than in the handler: multiple requests on a keep-alive connection share one
        # thread, and what shutdown has to wait for is "this thread has not finished", not "some request has not been answered".
        with self._inflight_done:
            self._inflight += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._inflight_done:
                self._inflight -= 1
                self._inflight_done.notify_all()

    def inflight_requests(self) -> int:
        with self._inflight_done:
            return self._inflight

    def await_inflight(self, timeout: float) -> int:
        """Wait for in-flight requests to finish; return how many are still unfinished at timeout (0 = all done).

        A count instead of a boolean: "how many are left" is the only way the shutdown log can say "this rolling update
        dropped N requests"; True/False cannot convey the scale."""
        deadline = time.monotonic() + timeout
        with self._inflight_done:
            while self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._inflight
                self._inflight_done.wait(remaining)
        return 0


def begin_shutdown(server: GracefulHTTPServer) -> None:
    """The signal handler only sets the event and spawns a thread; the real orchestration runs in the background.

    🔴 shutdown() **cannot** be called directly from the signal handler: it blocks until serve_forever exits,
       and serve_forever runs on the very thread the signal interrupted - calling it directly is a deadlock."""
    if _SHUTTING_DOWN.is_set():
        return
    _SHUTTING_DOWN.set()
    print("[control_plane] shutting down: draining traffic", flush=True)

    def _drain() -> None:
        # 1. /healthz is already reporting 503; wait for K8s to remove this Pod from Endpoints.
        time.sleep(SHUTDOWN_DRAIN_SECONDS)
        # 2. Only stop the accept loop. **This step does not wait for any in-flight request** - the waiting
        #    happens in finish_shutdown; see the GracefulHTTPServer description for why.
        print("[control_plane] draining done: closing listener", flush=True)
        server.shutdown()

    threading.Thread(target=_drain, name="shutdown", daemon=True).start()


def finish_shutdown(
    server: GracefulHTTPServer,
    reaper: threading.Thread | None = None,
) -> None:
    """The tail end after serve_forever returns. **Process exit must queue behind it.**

    serve_forever returning only means "no longer accepting new connections": requests already admitted each run in their own
    thread, and background provisionings run in create-* threads. Both are daemons - the moment the main thread
    returns they are cut off outright, with no chance to roll back.

    Boundary: the two waits share one deadline, so the worst case is SHUTDOWN_INFLIGHT_SECONDS
         rather than twice that - the total budget stays equal to terminationGracePeriodSeconds.
         When the wait runs out, report the count truthfully and give up: letting a stuck request drag the Pod to SIGKILL would be worse.
         The reaper's round is cut off the same way."""
    # Close the listening socket first. The join in ThreadingMixIn.server_close is a no-op for daemon threads
    # (see GracefulHTTPServer), so it does not block here.
    server.server_close()
    deadline = time.monotonic() + SHUTDOWN_INFLIGHT_SECONDS
    left = server.await_inflight(max(0.0, deadline - time.monotonic()))
    if left:
        print(
            f"[control_plane] giving up on {left} in-flight request(s) after "
            f"{SHUTDOWN_INFLIGHT_SECONDS:g}s",
            flush=True,
        )
    creating = await_pending_creations(max(0.0, deadline - time.monotonic()))
    if creating:
        # The IDs of these sandboxes have been returned to the client, which is polling right now. Cutting them off = the store row stays pending
        # and the Pod stays in the cluster; the client waits until the stale-pending cleanup.
        print(
            f"[control_plane] giving up on {creating} background creation(s); "
            f"they will be reconciled as stale pending",
            flush=True,
        )
    if reaper is not None:
        reaper.join(timeout=SHUTDOWN_REAPER_SECONDS)
        if reaper.is_alive():
            # Cutting delete_runtime off halfway leaves "Pod deleted, quota not returned" behind.
            # The two-way reconciliation covers it, but "this shutdown left residue" must be visible.
            print(
                f"[control_plane] reaper still running after "
                f"{SHUTDOWN_REAPER_SECONDS:g}s, leaving its round unfinished",
                flush=True,
            )


def install_signal_handlers(server: GracefulHTTPServer) -> None:
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: begin_shutdown(server))


# The Control Plane's authentication declaration for external routes. Triples of (method, sample path, credentials required).
#
# 🔴 Why this table exists: route dispatch is the if/elif chain in four do_* methods, each branch calling
# require_* on its own. In that structure "some branch forgot to authenticate" **cannot be found by anything** -
# that is how the 7 by-ID routes added in one batch leaked, and for how long they were open
# nobody knows. The table itself stops nobody; what stops it is the accompanying test: for every item marked True it
# sends a request without credentials to the router and asserts a 401. A new route that is not registered gets caught,
# and a route that is registered but whose branch forgot to call require_* gets caught too.
#
# Constraint: the sample path must really match the corresponding branch - a mismatch makes the request fall through to 404,
# and 404 is not 401. The test goes red on the spot rather than failing silently.
#
# The /v1/storage/* group takes any control-plane credential; what keeps one tenant out of another's objects is
# that the owner partition is derived from the credential rather than read from the request - see
# ApiHandler.resolve_object_owner. A new route in this group needs an entry in the owner-derivation table of
# tests/test_object_owner_derivation.py as well as one here.
ROUTE_AUTH: tuple[tuple[str, str, bool], ...] = (
    # Probe and operations plane: the only four routes reachable without credentials
    ("GET", "/livez", False),
    ("GET", "/readyz", False),
    ("GET", "/healthz", False),
    ("GET", "/metrics", False),
    # Sign-in discovery and the two OIDC redirect endpoints are public by
    # design: a browser reaches them precisely because it has no credential yet.
    # Logout is a mutation and must authenticate the session it ends (CSRF
    # token included).
    ("GET", "/v1/auth/methods", False),
    ("GET", "/v1/auth/oidc/login", False),
    ("GET", "/v1/auth/oidc/callback", False),
    ("POST", "/v1/auth/logout", True),
    # Control-plane credentials (static token / admin key / tenant key)
    ("GET", "/v1/whoami", True),
    ("GET", "/v1/templates", True),
    ("GET", "/v1/workspaces", True),
    ("GET", "/v1/sandboxes", True),
    ("GET", "/v1/monitoring", True),
    ("POST", "/v1/workspaces/resolve", True),
    ("POST", "/v1/workspaces", True),
    ("POST", "/v1/sandboxes", True),
    # Control-plane credentials + Workspace ownership
    ("GET", "/v1/workspaces/ws-aaaaaaaaaaaa/checkpoints", True),
    ("POST", "/v1/workspaces/ws-aaaaaaaaaaaa/checkpoints", True),
    ("POST", "/v1/workspaces/ws-aaaaaaaaaaaa/checkpoints/cp-1/restore", True),
    ("DELETE", "/v1/workspaces/ws-aaaaaaaaaaaa/checkpoints/cp-1", True),
    ("DELETE", "/v1/workspaces/ws-aaaaaaaaaaaa", True),
    # Control-plane credentials + Runtime ownership
    ("GET", "/v1/sandboxes/sb-000000000000", True),
    ("POST", "/v1/sandboxes/sb-000000000000/token", True),
    ("DELETE", "/v1/sandboxes/sb-000000000000", True),
    # Workspace read-only access: scoped token, or control-plane credentials + ownership
    ("GET", "/v1/workspaces/ws-aaaaaaaaaaaa/files/list", True),
    ("GET", "/v1/workspaces/ws-aaaaaaaaaaaa/files/read", True),
    ("GET", "/v1/workspaces/ws-aaaaaaaaaaaa/files/read-binary", True),
    ("GET", "/v1/workspaces/ws-aaaaaaaaaaaa/files/glob", True),
    ("GET", "/v1/workspaces/ws-aaaaaaaaaaaa/files/grep", True),
    # Scoped token only (control-plane credentials are **not** accepted on these)
    ("POST", "/v1/workspaces/ws-aaaaaaaaaaaa/files/write", True),
    ("POST", "/v1/workspaces/ws-aaaaaaaaaaaa/files/write-binary", True),
    ("POST", "/v1/workspaces/ws-aaaaaaaaaaaa/files/edit", True),
    ("POST", "/v1/workspaces/ws-aaaaaaaaaaaa/objects/import", True),
    ("POST", "/v1/workspaces/ws-aaaaaaaaaaaa/objects/export", True),
    ("POST", "/v1/sandboxes/sb-000000000000/mcp", True),
    # Object ticket (carries a jti, single-use)
    ("GET", "/v1/storage/content", True),
    ("PUT", "/v1/storage/content", True),
    # Object storage control plane - see the known exception above
    ("GET", "/v1/storage/objects", True),
    ("GET", "/v1/storage/objects/list", True),
    ("GET", "/v1/storage/objects/stat", True),
    ("GET", "/v1/storage/objects/versions", True),
    ("POST", "/v1/storage/objects", True),
    ("POST", "/v1/storage/tickets", True),
    ("DELETE", "/v1/storage/objects", True),
    # Management plane: admin key only, without X-Sandbox-Tenant
    ("GET", "/v1/admin/tenants", True),
    ("POST", "/v1/admin/tenants", True),
    ("DELETE", "/v1/admin/tenants/acme", True),
    ("POST", "/v1/admin/tenants/acme/status", True),
    ("GET", "/v1/admin/tenants/acme/keys", True),
    ("POST", "/v1/admin/tenants/acme/keys", True),
    ("GET", "/v1/admin/tenants/acme/owner-tenants", True),
    ("POST", "/v1/admin/tenants/acme/owner-tenants", True),
    ("DELETE", "/v1/admin/tenants/acme/owner-tenants/local", True),
    ("GET", "/v1/admin/keys", True),
    ("GET", "/v1/admin/audit", True),
    ("POST", "/v1/admin/keys", True),
    ("DELETE", "/v1/admin/keys/0123456789abcdef", True),
    ("GET", "/v1/admin/templates", True),
    ("POST", "/v1/admin/templates", True),
    ("DELETE", "/v1/admin/templates/tpl-1", True),
)
