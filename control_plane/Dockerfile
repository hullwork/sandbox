FROM golang:1.26.6-alpine@sha256:3889b425f035be855a72fb4755265311293b6d414521f0a519d819df32222d83 AS minio-client-build

WORKDIR /src
RUN apk add --no-cache patch=2.8-r0 \
    && wget -q -O /tmp/minio-mc.tar.gz \
      https://github.com/minio/mc/archive/77f82e18b5401a65958f1619df6ebb994634bd88.tar.gz \
    && echo "167415edd21bc29f5360943dac64272aa5cda0a39f3070b15cfeca671c43d975  /tmp/minio-mc.tar.gz" \
      | sha256sum -c - \
    && tar -xzf /tmp/minio-mc.tar.gz --strip-components=1 -C /src \
    && rm /tmp/minio-mc.tar.gz
COPY mc/dependency-overrides.patch /tmp/dependency-overrides.patch
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    patch -p1 < /tmp/dependency-overrides.patch \
    && go mod download \
    && go mod verify \
    && mkdir -p /out \
    && CGO_ENABLED=0 go build \
      -trimpath \
      -buildvcs=false \
      -tags kqueue \
      -ldflags "-s -w \
        -X github.com/minio/mc/cmd.Version=SANDBOX.PATCHED \
        -X github.com/minio/mc/cmd.CopyrightYear=2026 \
        -X github.com/minio/mc/cmd.ReleaseTag=SANDBOX.2025-11-06T16-25-29Z.patched \
        -X github.com/minio/mc/cmd.CommitID=77f82e18b5401a65958f1619df6ebb994634bd88 \
        -X github.com/minio/mc/cmd.ShortCommitID=77f82e18b540" \
      -o /out/mc .

FROM alpine:3.24.1@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b AS minio-client

RUN apk add --no-cache \
    libcrypto3=3.5.7-r0 \
    libssl3=3.5.7-r0
COPY --from=minio-client-build /out/mc /usr/bin/mc
COPY --from=minio-client-build /src/LICENSE /usr/share/licenses/minio-mc/LICENSE
USER 65532:65532
ENTRYPOINT ["/usr/bin/mc"]

FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

RUN addgroup -g 65532 control-plane \
    && adduser -D -u 65532 -G control-plane -h /nonexistent control-plane

# The control plane was originally a dependency-free standard-library service. psycopg
# and PyMySQL are an intentional tradeoff for relational tenant and ownership
# data; the image gains one C extension and two supply-chain entries. Hashes
# are pinned in requirements.lock.
COPY control_plane/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock \
    && find /usr/local/lib/python3.14/site-packages -maxdepth 1 \
      \( -name pip -o -name 'pip-*.dist-info' \) -exec rm -rf {} + \
    && find /usr/local/bin -maxdepth 1 -name 'pip*' -delete

COPY --chown=65532:65532 control_plane /app/control_plane
COPY --chown=65532:65532 workspace_contract.py capability_ticket.py /app/
WORKDIR /app
# Run the actual process entrypoint and import every shipped module during the
# build. This catches missing COPY entries and composition-root regressions.
# The database drivers are imported by name as well: SANDBOX_STORE_BACKEND
# selects them at runtime, so a driver missing from requirements.lock would
# otherwise surface only as 503s on a Pod whose /readyz is green.
RUN export SANDBOX_CONTROL_PLANE_ROLE=volume SANDBOX_CONTROL_PLANE_TOKEN=build-smoke SIGNING_KEY=build-smoke \
    WORKSPACE_ID_KEY=build-smoke OBJECT_STORE_ENDPOINT=http://127.0.0.1:1 \
    OBJECT_STORE_ACCESS_KEY=smoke OBJECT_STORE_SECRET_KEY=smoke VOLUME_AGENT_TOKEN=smoke \
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT= OTEL_EXPORTER_OTLP_ENDPOINT= \
    OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/json \
    SANDBOX_CONTROL_PLANE_HOST=127.0.0.1 SANDBOX_CONTROL_PLANE_PORT=18080 PYTHONUNBUFFERED=1; \
    python3 -m control_plane.server >/tmp/control-plane-smoke.log 2>&1 & smoke_pid=$! \
    && smoke_ready=0 \
    && for _attempt in $(seq 1 150); do \
         if grep -q "volume agent listening" /tmp/control-plane-smoke.log; then smoke_ready=1; break; fi; \
         if ! kill -0 "$smoke_pid" 2>/dev/null; then break; fi; \
         sleep 0.1; \
       done \
    && kill "$smoke_pid" 2>/dev/null || true \
    && wait "$smoke_pid" 2>/dev/null || true \
    && if [ "$smoke_ready" != 1 ]; then cat /tmp/control-plane-smoke.log; exit 1; fi \
    && rm /tmp/control-plane-smoke.log \
    && python3 -c "import control_plane.core, control_plane.server, control_plane.api, control_plane.oidc, control_plane.session, control_plane.tracing, control_plane.volume, control_plane.manifests, control_plane.reaper, control_plane.store, control_plane.metrics, control_plane.kube, control_plane.grafana_proxy, control_plane.runtime_driver, workspace_contract, capability_ticket; from control_plane.drivers import GVisorRuntimeDriver" \
    && python3 -c "import psycopg, pymysql" \
    && find /app -name '__pycache__' -type d -exec rm -rf {} +
COPY --from=minio-client /usr/bin/mc /usr/local/bin/mc
COPY --from=minio-client \
    /usr/share/licenses/minio-mc/LICENSE \
    /usr/share/licenses/minio-mc/LICENSE
# mc is an AGPL-3.0 runtime dependency built from a patched tree. Ship the
# corresponding-source pointer and the patch next to the license so image
# distributors can satisfy AGPL-3.0 sections 6 and 13 without this repository.
COPY mc/dependency-overrides.patch /usr/share/licenses/minio-mc/dependency-overrides.patch
RUN printf '%s\n' \
      'Component: MinIO Client (mc), AGPL-3.0' \
      'Upstream: https://github.com/minio/mc' \
      'Commit: 77f82e18b5401a65958f1619df6ebb994634bd88' \
      'Source tarball: https://github.com/minio/mc/archive/77f82e18b5401a65958f1619df6ebb994634bd88.tar.gz' \
      'Tarball SHA-256: 167415edd21bc29f5360943dac64272aa5cda0a39f3070b15cfeca671c43d975' \
      'Modification: dependency-overrides.patch in this directory (go.mod and go.sum only),' \
      '  kept in the Sandbox Platform repository as mc/dependency-overrides.patch' \
      'Corresponding source: download the tarball above and apply the patch with patch -p1;' \
      '  build steps are in control_plane/Dockerfile of the Sandbox Platform repository' \
      > /usr/share/licenses/minio-mc/SOURCE \
    && chmod 0444 /usr/share/licenses/minio-mc/SOURCE /usr/share/licenses/minio-mc/dependency-overrides.patch

USER 65532:65532
EXPOSE 8080

ENTRYPOINT ["python3", "-m", "control_plane.server"]
