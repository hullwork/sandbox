# Standalone deployment surface for this repository.
# The only cluster profile is single-VM Lima + kubeadm + Cilium + gVisor.
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

.PHONY: help doctor images test chart-lint chart-render dev-token control-plane-forward console-forward up-local status-local down-local destroy-local

help:
	@grep -E '^[a-z-]+:.* — ' Makefile | sed 's/:.* — / — /'

doctor: ## — verify local development prerequisites
	bash scripts/dev-doctor.sh

images: ## — build all local component images
	docker build -t sandbox-runtime:0.5.0 -f runtime/Dockerfile .
	docker build -t sandbox-file-service:0.3.0 -f file-service/Dockerfile .
	docker build -t sandbox-control-plane:0.7.0 -f control_plane/Dockerfile .
	docker build -t sandbox-console:0.1.0 -f console/Dockerfile console

test: ## — run standalone unit tests
	$(SANDBOX_PYTHON) -m unittest discover -s tests -p 'test_*.py'

chart-lint: ## — validate the independently deployable Helm package
	helm lint charts/sandbox

chart-render: ## — render the Helm package without an Infra checkout
	helm template sandbox charts/sandbox >/dev/null

dev-token: ## — print the local development control-plane token
	@KUBECONFIG=$(KUBECONFIG) SANDBOX_KUBE_CONTEXT=$(SANDBOX_KUBE_CONTEXT) bash scripts/print-dev-token.sh

control-plane-forward: ## — forward the Sandbox Control Plane API to 127.0.0.1:18080
	KUBECONFIG=$(KUBECONFIG) kubectl --context $(SANDBOX_KUBE_CONTEXT) --namespace sandbox-system \
		port-forward deploy/sandbox-control-plane 18080:8080

console-forward: ## — forward Console to 127.0.0.1:18081
	KUBECONFIG=$(KUBECONFIG) kubectl --context $(SANDBOX_KUBE_CONTEXT) --namespace sandbox-system \
		port-forward deploy/sandbox-console 18081:8080

up-local: ## — deploy the standalone Lima/kubeadm environment
	bash scripts/local-cluster.sh up

status-local: ## — show the standalone Lima/kubeadm environment
	bash scripts/local-cluster.sh status

down-local: ## — stop the Lima VM and retain its disk
	bash scripts/local-cluster.sh down

destroy-local: ## — delete the Lima VM, .sandbox state, and local images
	bash scripts/local-cluster.sh destroy
