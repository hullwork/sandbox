# The build context is the repository root, not runtime/, because
# sandbox_platform/safe_stdout.py and the inlined File Service live elsewhere in
# the repository.
FROM python:3.14-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

# The Agent session running in the sandbox must be able to verify the code and detect dependencies on the spot:
# - nodejs/npm: "Developing Angry Birds" two sessions (2026-08-20) writing JS front-end in the sandbox,
# `node -e` reports command not found, and can only be downgraded to python bracket balancing check——
# That's not syntax checking. Alpine's nodejs package **does not include** npm (disassembled starting from 3.19), which must be installed explicitly.
# - curl/git/jq/unzip/make: detect endpoints, process API JSON, and unpack all in the same session
# Wrap around with python. The built-in busybox (wget/tar/grep/sed/awk) does not need to be installed repeatedly.
# Deliberately not installed: gcc and other compilation chains (volume ~200MB, few scenarios), openssh (key management interface).
# All pins are the same as the above bash/tini version by version; dependabot only takes care of the base image above
# To upgrade digest and apk pin, you need to manually change this.
RUN apk add --no-cache bash=5.3.9-r1 tini=0.19.0-r3 \
        nodejs=24.18.1-r0 npm=11.12.1-r0 \
        curl=8.22.0-r0 git=2.54.0-r0 jq=1.8.2-r0 unzip=6.0-r16 make=4.4.1-r4 \
    && addgroup -g 65532 sandbox \
    && adduser -D -u 65532 -G sandbox -h /workspace sandbox \
    && mkdir -p /workspace /tmp \
    && chown -R 65532:65532 /workspace /tmp

# Four-piece document generation set (runtime dependency of pptx/docx/xlsx/pdf builtin skills):
# - font-noto-cjk: Chinese rendering. The office system only references the font name (parsed on the viewer side),
# PDFs are embedded in TTF by fpdf2, both taken from the system font directory.
# - poppler-utils: pdftoppm/pdfinfo/pdffonts, QA use of pdf skill (transfer image to render proof
# + font embedding check). Note that alpine's poppler package only contains libraries and does not include these three tools. The tools are in
# in poppler-utils. There is no LibreOffice in the sandbox, office rendering level QA cannot be done, and it is a known downgrade.
# apk is also pinned version by version (same rule as bash/tini above).
RUN apk add --no-cache font-noto-cjk=0_git20220127-r1 poppler-utils=25.12.0-r1
# The whole document-processing stack installs as pure-Python/musl wheels (verified). reportlab has no musllinux wheel,
# so PDF uses fpdf2; fpdf2 >=2.7 supports .ttc collection fonts directly. A smoke import runs right after installation:
# a missing dependency fails the build instead of the Pod. The trailing cache cleanup is best-effort; grouping it with {}
# keeps its `|| true` from also swallowing a failure of the preceding pip/import.
RUN pip install --no-cache-dir python-pptx==1.0.2 python-docx==1.2.0 \
        openpyxl==3.1.5 fpdf2==2.8.8 \
    && python3 -c "import pptx, docx, openpyxl, fpdf; print('doc-libs-ok')" \
    # Runtime intentionally has no package manager; removing pip also removes
    # its vendored dependencies and installation attack surface.
    && find /usr/local/lib/python3.14/site-packages -maxdepth 1 \
      \( -name pip -o -name 'pip-*.dist-info' \) -exec rm -rf {} + \
    && find /usr/local/bin -maxdepth 1 -name 'pip*' -delete \
    && { find /root/.cache -maxdepth 1 -mindepth 1 -exec rm -rf {} + 2>/dev/null || true; }

# Listing files explicitly makes omissions visible at build time. New modules
# are easy to miss; update these COPY lines together and rely on the build-time
# smoke test and infrastructure contract tests below. Source paths are relative
# to the repository root because all build call sites use that context.
COPY --chown=65532:65532 runtime/runtime_server.py /app/runtime_server.py
COPY --chown=65532:65532 runtime/shell_sessions.py /app/shell_sessions.py
COPY --chown=65532:65532 file-service/file_service.py /app/file_service.py
COPY --chown=65532:65532 workspace_contract.py /app/workspace_contract.py
COPY --chown=65532:65532 capability_ticket.py /app/capability_ticket.py
# runtime_server.py installs safe_stdout in __main__ so a blocked log consumer
# cannot freeze HTTP threads before a response is sent. The module lives in the
# sandbox_platform package; only the two files the import needs are copied so
# the SDK does not enter the Runtime image. Unit tests import it directly, while
# an image omission would only surface after container startup.
COPY --chown=65532:65532 sandbox_platform/__init__.py sandbox_platform/safe_stdout.py /app/sandbox_platform/
# Build smoke test: close the entrypoint import chain here instead of waiting
# for a Pod crash. List every module explicitly; optional import fallbacks can
# hide a missing transitive module, and safe_stdout is imported only in
# __main__. Infrastructure contract tests pin these requirements.
RUN SANDBOX_CAPABILITY_KEY=build-smoke SANDBOX_ID=sb-build-smoke \
    WORKSPACE_ID=ws-build-smoke \
    python3 -c "import sys; sys.path.insert(0, '/app'); import runtime_server, shell_sessions, file_service, workspace_contract, capability_ticket; from sandbox_platform import safe_stdout" \
    && find /app -name '__pycache__' -type d -exec rm -rf {} +

USER 65532:65532
WORKDIR /workspace
EXPOSE 8080

ENTRYPOINT ["/sbin/tini", "--", "python3", "/app/runtime_server.py"]
