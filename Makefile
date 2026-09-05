# Standalone deployment surface for this repository.
# The local profile is one Lima + kubeadm cluster: trusted system services on
# the control-plane node, gVisor Runtime workloads on a scalable tainted worker pool.
# Image build contexts are deliberately rooted in this repository.
SHELL := /usr/bin/env bash
SANDBOX_KUBE_CONTEXT ?= sandbox-local
# scripts/local-cluster.sh writes the cluster-admin kubeconfig here, not into
# ~/.kube/config. Mirror its defaults so kubectl-based targets find the context.
SANDBOX_STATE_DIR ?= $(CURDIR)/.sandbox
SANDBOX_KUBECONFIG ?= $(SANDBOX_STATE_DIR)/kubeconfig
export KUBECONFIG := $(SANDBOX_KUBECONFIG)
# README tells a newcomer to install into ./.venv, because a system-wide
# `pip install` is an error on every PEP 668 distribution. Prefer that
# interpreter here so the install step and the test step agree: with plain
# `python3` the suite runs against whatever the system happens to have, and the
# ten modules that need PyYAML fail on an import rather than on anything about
# this repository.
SANDBOX_PYTHON ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
SANDBOX_IMAGE_BUILD_JOBS ?= 4
SANDBOX_BUILD_LOG_DIR ?= $(SANDBOX_STATE_DIR)/logs/images
DOCKER_BUILD_PROGRESS ?= plain
SANDBOX_BOOTSTRAP_STAMP ?= .venv/.sandbox-bootstrap

.PHONY: help bootstrap quickstart acceptance smoke-local e2e-local verify verify-manifests doctor images image-runtime image-file-service image-control-plane image-console test chart-lint chart-render dev-token control-plane-forward console-forward up-local status-local scale-workers down-local destroy-local

help:
	@grep -E '^[a-z-]+:.* — ' Makefile | sed 's/:.* — / — /'

doctor: ## — verify local development prerequisites
	bash scripts/dev-doctor.sh

bootstrap: $(SANDBOX_BOOTSTRAP_STAMP) ## — create .venv and install the SDK plus test dependencies

$(SANDBOX_BOOTSTRAP_STAMP): pyproject.toml
	python3 -m venv .venv
	PIP_DISABLE_PIP_VERSION_CHECK=1 .venv/bin/python -m pip install -e '.[test]'
	@touch $(SANDBOX_BOOTSTRAP_STAMP)

quickstart: ## — go from prerequisites to a live gVisor proof and KPI summary
	bash scripts/quickstart.sh

acceptance: ## — install, prove runtime value, and run every source and live E2E gate
	$(MAKE) --no-print-directory quickstart
	$(MAKE) --no-print-directory verify
	$(MAKE) --no-print-directory e2e-local

smoke-local: ## — prove gVisor isolation, persistence, fail-closed behavior, and metrics
	$(SANDBOX_PYTHON) scripts/smoke-local.py

e2e-local: ## — run all network, Runtime, storage, restart, and adversarial E2E checks
	SANDBOX_KUBECONFIG=$(KUBECONFIG) \
	SANDBOX_KUBE_CONTEXT=$(SANDBOX_KUBE_CONTEXT) \
	SANDBOX_EXPECT_DEDICATED_RUNTIME_NODE=1 \
	PYTHON=$(SANDBOX_PYTHON) bash scripts/run-all-e2e.sh

verify: ## — run the complete source, package, Console, and manifest gate with logs
	bash scripts/verify.sh

verify-manifests:
	bash -n scripts/*.sh
	@for overlay in k8s overlays/rwo-single-node overlays/local overlays/eks overlays/external-deps; do \
		kubectl kustomize "$$overlay" >/dev/null || exit; \
	done
	$(MAKE) --no-print-directory chart-lint
	$(MAKE) --no-print-directory chart-render

images: image-runtime image-file-service image-control-plane image-console ## — build all local component images

image-runtime:
	@bash scripts/build-image.sh sandbox-runtime:0.5.0 -f runtime/Dockerfile .

image-file-service:
	@bash scripts/build-image.sh sandbox-file-service:0.3.0 -f file-service/Dockerfile .

image-control-plane:
	@bash scripts/build-image.sh sandbox-control-plane:0.7.0 -f control_plane/Dockerfile .

image-console:
	@bash scripts/build-image.sh sandbox-console:0.1.0 -f console/Dockerfile console

test: ## — run standalone unit tests
	$(SANDBOX_PYTHON) -m unittest discover -s tests -p 'test_*.py'

chart-lint: ## — validate the independently deployable Helm package
	helm lint charts/sandbox

chart-render: ## — render the Helm package without any other repository
	helm template sandbox charts/sandbox >/dev/null

dev-token: ## — print the local development control-plane token
	@KUBECONFIG=$(KUBECONFIG) SANDBOX_KUBE_CONTEXT=$(SANDBOX_KUBE_CONTEXT) bash scripts/print-dev-token.sh

control-plane-forward: ## — forward the Sandbox Control Plane API to 127.0.0.1:18080
	KUBECONFIG=$(KUBECONFIG) kubectl --context $(SANDBOX_KUBE_CONTEXT) --namespace sandbox-system \
		port-forward deploy/sandbox-control-plane 18080:8080

console-forward: ## — forward Console to 127.0.0.1:18081
	@echo "Console: http://127.0.0.1:18081"
	@echo "Local login key (run in another terminal): make --no-print-directory dev-token"
	KUBECONFIG=$(KUBECONFIG) kubectl --context $(SANDBOX_KUBE_CONTEXT) --namespace sandbox-system \
		port-forward deploy/sandbox-console 18081:8080

up-local: ## — deploy the standalone Lima/kubeadm environment
	bash scripts/local-cluster.sh up

status-local: ## — show the standalone Lima/kubeadm environment
	bash scripts/local-cluster.sh status

scale-workers: ## — resize the Runtime worker pool with WORKERS=N
	@test -n "$(WORKERS)" || { echo 'usage: make scale-workers WORKERS=N' >&2; exit 2; }
	bash scripts/local-cluster.sh scale-workers "$(WORKERS)"

down-local: ## — stop the control plane and all Runtime workers; retain disks
	bash scripts/local-cluster.sh down

destroy-local: ## — delete all profile VMs, .sandbox state, and local images
	bash scripts/local-cluster.sh destroy
