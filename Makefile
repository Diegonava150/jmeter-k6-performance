# One workload, two engines, one gate.
#
#   make up              start the hermetic stack
#   make load            closed profile: both engines + cross-verify
#   make load-open       open profile: fixed arrival rate, service can saturate
#   make load-all        both profiles
#   make prove-teeth     inject latency, assert both engines catch it
#   make prove-divergence desync the engines, assert the gate catches THAT
#   make test            everything checkable without Docker
#   make down            tear everything down

SHELL      := /bin/bash
PY         ?= python3
REPORTS    := reports
PROFILE    ?= closed
MULT       ?= 1

PROPS      := $(REPORTS)/jmeter.props
JTL        := $(REPORTS)/jmeter.jtl
K6_RAW     := $(REPORTS)/k6_raw.json
JM_SUM     := $(REPORTS)/jmeter.summary.json
K6_SUM     := $(REPORTS)/k6.summary.json

.PHONY: all up down load load-open load-all verify jmeter k6 prove-teeth \
        prove-divergence prove-reachable clean self-test test report status

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

$(REPORTS):
	@mkdir -p $(REPORTS)

# ----------------------------------------------------------------- runs ----
#
# Both engines take PROFILE and MULT. MULT exists only for prove-divergence,
# which runs the two engines at deliberately different loads.

# JMeter in non-GUI mode. -q layers each property file ON TOP of the shipped
# jmeter.properties, so every __P() in workload.jmx resolves to a value that came
# from slo.json. (-p would REPLACE jmeter.properties wholesale and silently take
# the report generator's own defaults with it.)
jmeter: | $(REPORTS)
	@echo ">> jmeter  profile=$(PROFILE) mult=$(MULT)"
	@$(PY) tools/gen_jmeter_props.py $(PROPS) $(PROFILE) $(MULT)
	@rm -f $(JTL)
	jmeter -n -t jmeter/workload.jmx \
	       -q jmeter/user.properties \
	       -q $(PROPS) \
	       -l $(JTL) \
	       -j $(REPORTS)/jmeter.log
	@$(PY) tools/summarize.py jmeter $(JTL) $(JM_SUM) $(PROFILE)

# k6 reads slo.json itself. --out json gives us the raw per-request stream that
# the shared summarizer scores; k6's own threshold verdict still prints, and is
# expected to agree with it.
k6: | $(REPORTS)
	@echo ">> k6      profile=$(PROFILE) mult=$(MULT)"
	@rm -f $(K6_RAW)
	-k6 run -e PROFILE=$(PROFILE) -e LOAD_MULTIPLIER=$(MULT) \
	        --out json=$(K6_RAW) --quiet k6/workload.js
	@$(PY) tools/summarize.py k6 $(K6_RAW) $(K6_SUM) $(PROFILE)

# Engines run back to back, never concurrently: two load generators on one box
# contend for the same CPU and NIC, and then the divergence check is measuring
# the laptop instead of the service.
load: jmeter k6 verify

load-open:
	@$(MAKE) --no-print-directory load PROFILE=open

load-all: load load-open

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
	@$(MAKE) --no-print-directory jmeter k6 PROFILE=$(PROFILE) || true
	@echo ">> both engines must have FAILED"
	@$(PY) tools/cross_verify.py $(JM_SUM) $(K6_SUM) --expect-fail; \
	  rc=$$?; $(PY) tools/toxic.py remove; exit $$rc

# ----------------------------------------------------- prove-divergence ----
#
# The same argument, aimed at the check this repo is actually about.
#
# prove-teeth shows the SLO thresholds can fail. It says nothing about the
# divergence bands, which are the part that decides whether a green run is
# trustworthy at all. So: run JMeter as configured and k6 at a deliberately
# different load, against a perfectly healthy service. Both engines will very
# likely still be inside the SLO -- that is the point. A build that only checked
# thresholds would wave this through, and it must not, because the two runs are
# not measurements of the same thing.

prove-divergence:
	@echo ">> running the engines at deliberately different loads"
	@$(MAKE) --no-print-directory jmeter PROFILE=$(PROFILE) MULT=1
	@$(MAKE) --no-print-directory k6 PROFILE=$(PROFILE) \
	    MULT=$$($(PY) tools/slo.py divergence_probe.load_multiplier)
	@echo ">> the gate must reject this run on the divergence bands alone"
	@$(PY) tools/cross_verify.py $(JM_SUM) $(K6_SUM) --expect-divergence

# --------------------------------------------------------------- misc ------

# Everything that can be checked without Docker or a load generator. Runs first
# in CI: if the summarizer is not engine-blind, or the gate does not reject what
# it claims to reject, or a declared threshold cannot fire, nothing measured
# downstream means anything.
test: self-test prove-reachable
	@echo ">> gate tests"
	@$(PY) tools/test_gate.py

self-test:
	@echo ">> summarizer self-test"
	@$(PY) tools/summarize.py --self-test
	@echo ">> reachability self-test"
	@$(PY) tools/prove_reachable.py --self-test

prove-reachable:
	@$(PY) tools/prove_reachable.py

report: $(JTL)
	@rm -rf $(REPORTS)/html
	jmeter -g $(JTL) -o $(REPORTS)/html -q jmeter/user.properties
	@echo "open $(REPORTS)/html/index.html"

clean:
	rm -rf $(REPORTS)/* jmeter.log
	@touch $(REPORTS)/.gitkeep
