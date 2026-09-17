SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

ROOT_DIR := $(shell git rev-parse --show-toplevel)
SED := $(shell command -v gsed 2>/dev/null || command -v sed)

# Default to latest commit if COMMIT is not specified
COMMIT ?= $(shell git ls-remote https://github.com/gardenlinux/gardenlinux.git HEAD | cut -f1)

.PHONY: prepare update clean help ccloud-help

ccloud-help:
	@echo
	@echo "CCloud Custom targets:"
	@echo "  prepare                Initialize submodules and prepare environment"
	@echo "  update [COMMIT=<hash>] Update Garden Linux submodule (to specific commit or latest)"
	@echo "  clean                  Remove Garden Linux submodule and reset environment"
	@echo

help: ccloud-help

-include gardenlinux/Makefile

prepare:
	git submodule update --init --recursive

update:
	# update gardenlinux submodule to specified or latest commit
	cd $(ROOT_DIR)/gardenlinux && git fetch && git checkout $(COMMIT) && cd ..
	git add gardenlinux

	# update workflow commit references
	$(SED) -i -E 's|(gardenlinux/gardenlinux/.github/workflows/[^@]*)@[0-9a-f]{40}|\1@$(COMMIT)|g' $(ROOT_DIR)/.github/workflows/*.y*ml

	# update features
	mkdir -p $(ROOT_DIR)/features
	for feature in $$(ls $(ROOT_DIR)/gardenlinux/features); do \
		if [ -L "$(ROOT_DIR)/features/$$feature" ]; then \
			rm "$(ROOT_DIR)/features/$$feature"; \
		fi; \
		if [ ! -e "$(ROOT_DIR)/features/$$feature" ]; then \
			cd $(ROOT_DIR)/features && ln -s "../gardenlinux/features/$$feature" "$$feature"; \
		fi; \
	done

	# update symlinks in bin folder
	mkdir -p $(ROOT_DIR)/bin
	for script in $$(ls -A $(ROOT_DIR)/gardenlinux/bin); do \
		if [ "$$script" == "." ] || [ "$$script" == ".." ]; then \
			continue; \
		fi; \
		if [ -L "$(ROOT_DIR)/bin/$$script" ]; then \
			rm "$(ROOT_DIR)/bin/$$script"; \
		fi; \
		if [ ! -e "$(ROOT_DIR)/bin/$$script" ]; then \
			cd $(ROOT_DIR)/bin && ln -s "../gardenlinux/bin/$$script" "$$script"; \
		fi; \
	done

	# update builder image
	new_builder_image=$$(grep -m1 '^container_image=' $(ROOT_DIR)/gardenlinux/build | cut -d= -f2); \
	current_builder_image=$$(grep -m1 '^container_image=' $(ROOT_DIR)/build | cut -d= -f2); \
	if [ "$$new_builder_image" != "$$current_builder_image" ]; then \
		$(SED) -i -E 's|^container_image=.*|container_image=$$new_builder_image|' $(ROOT_DIR)/build; \
	fi

clean:
	git reset --soft
	rm -rf $(ROOT_DIR)/gardenlinux
	git submodule update --init --recursive
