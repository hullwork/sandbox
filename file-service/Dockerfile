# The build context is the repository root, not file-service/, because
# safe_stdout.py lives in the repository's sandbox_platform package.
FROM python:3.14-alpine@sha256:c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc

#tini: When there is no init, python is PID 1, and the kernel directly responds to the SIG_DFL signal of PID 1
#Ignore - all SIGTERMs fail, each Pod must wait for the entire terminationGracePeriodSeconds
#It was SIGKILL (actually measured 10s, 1s with init). When Control Plane deletes a Pod, it passes gracePeriodSeconds: 0
#This matter is covered up, but node emptying, eviction, and manual kubectl delete all use the default grace period.
#The version is consistent with sandbox/runtime/Dockerfile (same base digest).
RUN apk add --no-cache tini=0.19.0-r3 \
    && addgroup -g 65532 workspace \
    && adduser -D -u 65532 -G workspace -h /workspace workspace \
    && mkdir -p /workspace /tmp \
    && chown -R 65532:65532 /workspace /tmp \
    # The service image has no runtime installation needs; remove pip and its
    # vendored dependencies.
    && find /usr/local/lib/python3.14/site-packages -maxdepth 1 \
      \( -name pip -o -name 'pip-*.dist-info' \) -exec rm -rf {} + \
    && find /usr/local/bin -maxdepth 1 -name 'pip*' -delete

COPY --chown=65532:65532 file-service/file_service.py /app/file_service.py
COPY --chown=65532:65532 file-service/gc_workspaces.py /app/gc_workspaces.py
COPY --chown=65532:65532 workspace_contract.py /app/workspace_contract.py
COPY --chown=65532:65532 capability_ticket.py /app/capability_ticket.py
# file_service.py installs safe_stdout in __main__ so a blocked log consumer
# cannot freeze HTTP threads. The module lives in the sandbox_platform package;
# only the two files the import needs are copied. An image omission would only
# surface after container startup.
COPY --chown=65532:65532 sandbox_platform/__init__.py sandbox_platform/safe_stdout.py /app/sandbox_platform/

USER 65532:65532
WORKDIR /workspace
EXPOSE 8081

ENTRYPOINT ["/sbin/tini", "--", "python3", "/app/file_service.py"]
