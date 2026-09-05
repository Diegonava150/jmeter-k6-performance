# One workload, two engines, one gate.
#
#   make up            start the hermetic stack
#   make load          run BOTH engines and cross-verify   <- the main entry point
#   make prove-teeth   inject latency, assert both engines catch it
#   make down          tear everything down

SHELL      := /bin/bash
PY         ?= python3
REPORTS    := reports
PROPS      := $(REPORTS)/jmeter.props
JTL        := $(REPORTS)/jmeter.jtl
K6_RAW     := $(REPORTS)/k6_raw.json
JM_SUM     := $(REPORTS)/jmeter.summary.json
K6_SUM     := $(REPORTS)/k6.summary.json

.PHONY: all up down load verify jmeter k6 prove-teeth clean self-test test report status

all: load

# ---------------------------------------------------------------- stack ----

up:
	docker compose up -d
	$(PY) tools/wait_for_stack.py

down:
	-$(PY) tools/toxic.py remove 2>/dev/null
	docker compose down -v

status:
	@docker compose ps --format "table {{.Service}}\t{{.Status}}"
	@$(PY) tools/toxic.py status

# ----------------------------------------------------------------- runs ----

$(PROPS): slo.json tools/gen_jmeter_props.py | $(REPORTS)
	@$(PY) tools/gen_jmeter_props.py $@

$(REPORTS):
	@mkdir -p $(REPORTS)

# JMeter in non-GUI mode. -q layers each property file ON TOP of the shipped
# jmeter.properties, so every __P() in workload.jmx resolves to a value that came
# from slo.json. (-p would REPLACE jmeter.properties wholesale and silently take
# the report generator's own defaults with it.)
jmeter: $(PROPS)
	@echo ">> jmeter"
	@rm -f $(JTL)
	jmeter -n -t jmeter/workload.jmx \
	       -q jmeter/user.properties \
	       -q $(PROPS) \
	       -l $(JTL) \
	       -j $(REPORTS)/jmeter.log
	@$(PY) tools/summarize.py jmeter $(JTL) $(JM_SUM)

# k6 reads slo.json itself. --out json gives us the raw per-request stream that
# the shared summarizer scores; k6's own threshold verdict still prints, and is
# expected to agree with it.
k6: | $(REPORTS)
	@echo ">> k6"
	@rm -f $(K6_RAW)
	-k6 run --out json=$(K6_RAW) --quiet k6/workload.js
	@$(PY) tools/summarize.py k6 $(K6_RAW) $(K6_SUM)

# Engines run back to back, never concurrently: two load generators on one box
# contend for the same CPU and NIC, and then the divergence check is measuring
# the laptop instead of the service.
load: jmeter k6 verify

verify:
	@$(PY) tools/cross_verify.py $(JM_SUM) $(K6_SUM)

# --------------------------------------------------------------- teeth -----
#
# Mutation testing for a performance gate. Inject a fault big enough that any
# honest SLO check must trip, then require BOTH engines to report FAIL. An
# engine that still says PASS is not passing, it is blind, and a blind gate is
# the failure mode that matters: it is green for exactly as long as it is
# useless.

prove-teeth:
	@echo ">> injecting fault"
	@$(PY) tools/toxic.py add
	@$(MAKE) --no-print-directory jmeter k6 || true
	@echo ">> both engines must have FAILED"
	@$(PY) tools/cross_verify.py $(JM_SUM) $(K6_SUM) --expect-fail; \
	  rc=$$?; $(PY) tools/toxic.py remove; exit $$rc

# --------------------------------------------------------------- misc ------

# Everything that can be checked without Docker or a load generator. Runs first
# in CI: if the summarizer is not engine-blind, or the gate does not reject what
# it claims to reject, nothing measured downstream means anything.
test: self-test
	@echo ">> gate tests"
	@$(PY) tools/test_gate.py

self-test:
	@echo ">> summarizer self-test"
	@$(PY) tools/summarize.py --self-test

report: $(JTL)
	@rm -rf $(REPORTS)/html
	jmeter -g $(JTL) -o $(REPORTS)/html -q jmeter/user.properties
	@echo "open $(REPORTS)/html/index.html"

clean:
	rm -rf $(REPORTS)/* jmeter.log
	@touch $(REPORTS)/.gitkeep
