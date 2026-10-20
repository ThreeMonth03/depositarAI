.DEFAULT_GOAL := help
.PHONY: help search-ci test ci

help:
	@printf '%s\n' \
	  'Private trial: make product-up-private CONFIG=/absolute/path/to/product.json' \
	  'Product steps: make product-<command> CONFIG=/absolute/path/to/product.json' \
	  'Start here: docs/guide/operations.md' \
	  'Remote frontend: docs/SEARCH_WEBSITE.md' \
	  'Checks: make test | make search-web-ci'

search-ci:
	./scripts/search-ci.sh

test: search-ci

ci: search-ci

.PHONY: search-web-ci
search-web-ci:
	bash scripts/test-search-web.sh

# Product staging commands never replace an active catalog or publish ports.
.PHONY: product-doctor product-sync product-status product-prepare-index product-start-index product-import-index product-reconcile-index product-build-vectors product-stage-catalog product-build-images product-build-catalog-images product-prepare-query product-prepare-secrets product-start-inference product-start-release product-prepare-gateway
product-doctor product-sync product-status product-prepare-index product-start-index product-import-index product-reconcile-index product-build-vectors product-stage-catalog product-build-images product-build-catalog-images product-prepare-query product-prepare-secrets product-start-inference product-start-release product-prepare-gateway:
	@test -n "$(CONFIG)" || { echo "CONFIG=/absolute/path/to/product.json is required" >&2; exit 2; }
	python3 scripts/depositarai.py --config "$(CONFIG)" $(patsubst product-%,%,$@)

.PHONY: product-package
.PHONY: product-up-private product-activate-gateway
product-up-private product-activate-gateway:
	@test -n "$(CONFIG)" || { echo "CONFIG=/absolute/path/to/product.json is required" >&2; exit 2; }
	python3 scripts/depositarai.py --config "$(CONFIG)" $(patsubst product-%,%,$@)

product-package:
	@test -n "$(CONFIG)" -a -n "$(DESTINATION)" -a -n "$(SCANNER)" || { echo "CONFIG, DESTINATION and SCANNER absolute paths are required" >&2; exit 2; }
	python3 scripts/depositarai.py --config "$(CONFIG)" package-product --destination "$(DESTINATION)" --scanner "$(SCANNER)"
