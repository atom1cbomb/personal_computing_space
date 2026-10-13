# Composite Heterogeneous Scheduling Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and execute a reproducible 100-image, 301-node Loom inspection DAG across two- and three-Slave controlled heterogeneous topologies, comparing round-robin, least-loaded, and earliest-finish scheduling.

**Architecture:** Extend Slave registration from one scalar scheduling profile to a default plus per-package profiles, while preserving the existing flat profile as a compatibility input. Add a third isolated Slave and a real HTTP image-preprocessing capability; drive a deterministic preprocess → YOLO → risk → aggregate DAG through Loom and collect Run, Attempt, event, calibration, and business-output evidence.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest, Docker Compose, PostgreSQL, MinIO, Pillow, Ultralytics YOLO11n, Loom MCP/HTTP APIs.

**Spec:** `docs/superpowers/specs/2026-10-13-composite-heterogeneous-scheduling-experiment-design.md`

## Global Constraints

- Use exactly 100 manifest images for formal runs and exactly 301 dynamic nodes.
- Compare `round_robin_v1`, `least_loaded_v1`, and `earliest_finish_v1` under identical workload inputs.
- Support A+B and A+B+C topologies without sharing Slave databases or runtime state volumes.
- Prove heterogeneity through both Docker configuration evidence and pinned Loom calibration Attempts.
- Keep Agent/DeepSeek out of timed runs; the benchmark DAG is deterministic.
- Preserve failures in raw results and never rank a strategy using only successful samples.
- Work only on local branch `feat/pluggable-heterogeneous-scheduling`; do not push a remote branch.
- Use `PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest ...` so orchestration preflight can find the pinned Pyright executable.

---

### Task 1: Per-package Slave scheduling profiles

**Files:**
- Modify: `loom_v2/settings.py`
- Modify: `loom_v2/slave/app.py`
- Modify: `loom_v2/testing/observer.py`
- Modify: `loom_v2/driver/orchestration_runtime.py`
- Modify: `tests/driver/test_scheduling.py`
- Modify: `tests/driver/test_dynamic_orchestration_runtime.py`

**Interfaces:**
- Consumes: `CapabilityPackageVersion.package_id` and Observer `slave_capabilities[slave_id]["scheduling"]`.
- Produces: `Settings.scheduling_profiles: dict[str, SchedulingProfile]` and package-specific `SchedulingCandidate` values with flat-profile fallback.

- [ ] **Step 1: Write failing settings and selection tests**

Add tests proving that JSON environment data parses into default/package profiles and that selecting a `yolo11n-http` package uses its profile rather than the default. Hand-check expected candidate values: A uses default `1000 ms`, B uses package value `200 ms`.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest \
  tests/driver/test_scheduling.py \
  tests/driver/test_dynamic_orchestration_runtime.py::test_target_selection_uses_package_specific_slave_profile -q
```

Expected: fail because structured profiles do not exist and the runtime reads only flat keys.

- [ ] **Step 3: Implement structured profile parsing and registration**

Introduce a small Pydantic `SchedulingProfile` model with positive `capacity` and non-negative time values. Parse `LOOM_SCHEDULING_PROFILES` as:

```json
{"default":{"capacity":1,"service_time_ms":1000,"cold_start_ms":0},"packages":{"yolo11n-http":{"capacity":1,"service_time_ms":200,"cold_start_ms":1500}}}
```

Slave registration emits this structured value. The Driver resolves `packages[package.package_id]`, then `default`, then the legacy flat mapping.

- [ ] **Step 4: Run targeted and regression tests**

Run the command from Step 2, then:

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest \
  tests/driver/test_scheduling.py \
  tests/driver/test_dynamic_orchestration_runtime.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit locally**

```bash
git add loom_v2/settings.py loom_v2/slave/app.py loom_v2/testing/observer.py \
  loom_v2/driver/orchestration_runtime.py tests/driver/test_scheduling.py \
  tests/driver/test_dynamic_orchestration_runtime.py
git commit -m "feat: support per-package scheduling profiles"
```

### Task 2: Three-Slave controlled topology

**Files:**
- Modify: `deploy/docker-compose.yml`
- Modify: `deploy/docker-compose.heterogeneous.yml`
- Create: `tests/deploy/test_heterogeneous_compose.py`

**Interfaces:**
- Consumes: standard Slave image, internal API secret, runtime plugin directory, package contracts, Docker socket.
- Produces: independent `slave-c` and `slave-c-db` services plus `slave-c-runtime-plugin-sockets` volume.

- [ ] **Step 1: Write a failing Compose behavior test**

Load both YAML files with the existing YAML dependency or invoke `docker compose config --format json`. Assert that `slave-c` has its own database URL, port 8083 endpoint, state volume, CPU limit, and that all three Slaves receive distinct structured scheduling profiles.

- [ ] **Step 2: Run the test and verify RED**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/deploy/test_heterogeneous_compose.py -q
```

Expected: fail because `slave-c` is absent.

- [ ] **Step 3: Add Slave C and topology profiles**

Add `slave-c-db`, `slave-c`, its volume, explicit endpoint URL, and resource limits. Configure A/B/C as 0.50/1.50/3.00 Slave CPUs and 0.40/1.00/2.50 service-container CPUs. Add environment-controlled profiles for preprocess, YOLO, risk, and aggregate packages.

- [ ] **Step 4: Validate Compose and tests**

```bash
LOOM_SCHEDULING_POLICY=earliest_finish_v1 \
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.heterogeneous.yml config --quiet
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/deploy/test_heterogeneous_compose.py -q
```

Expected: both commands exit 0.

- [ ] **Step 5: Commit locally**

```bash
git add deploy/docker-compose.yml deploy/docker-compose.heterogeneous.yml tests/deploy/test_heterogeneous_compose.py
git commit -m "feat: add third controlled heterogeneous slave"
```

### Task 3: Reproducible preprocessing and YOLO services

**Files:**
- Create: `capability_services/image_preprocess_http/server.py`
- Create: `capability_services/image_preprocess_http/Dockerfile`
- Create: `capability_services/image_preprocess_http/requirements.txt`
- Create: `capability_services/image_preprocess_http/.dockerignore`
- Create: `capability_services/image_preprocess_http/schemas/preprocess-input.json`
- Create: `capability_services/image_preprocess_http/schemas/preprocess-output.json`
- Create: `capability_services/image_preprocess_http/contracts/preprocess.io.json`
- Create: `capability_services/image_preprocess_http/package.example.json`
- Create: `capability_services/image_preprocess_http/README.md`
- Create: `capability_services/yolo11n_http/*`
- Create: `tests/capability_services/test_image_preprocess_http.py`
- Create: `tests/capability_services/test_yolo_contract.py`

**Interfaces:**
- Consumes: `{request_id, image_id, image_base64, max_edge, jpeg_quality}`.
- Produces: normalized YOLO-compatible payload plus dimensions, byte counts, and SHA-256.

- [ ] **Step 1: Write failing preprocessing behavior tests**

Use an in-memory JPEG fixture. Assert `/healthz`, EXIF-safe RGB normalization, longest-edge limit, stable IDs, decodable returned JPEG, and invalid Base64 rejection. The test exercises the real FastAPI app through ASGI transport.

- [ ] **Step 2: Verify RED**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/capability_services/test_image_preprocess_http.py -q
```

Expected: import failure because the service does not exist.

- [ ] **Step 3: Implement the minimal service and contracts**

Use FastAPI, Pydantic and Pillow. Perform real decode, orientation normalization, RGB conversion, bounded resize and JPEG encoding; do not add artificial delay. Define schemas and package manifests matching `/invoke` and `/healthz`.

- [ ] **Step 4: Bring the existing local YOLO service into the experiment branch**

Recreate the already-used `capability_services/yolo11n_http` files from the main working tree without modifying that main working tree. Add a contract test that validates representative request/response documents against its JSON schemas.

- [ ] **Step 5: Run service tests and build both images**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/capability_services -q
docker build -t localhost:5001/loom/image-preprocess-http:0.1.0 capability_services/image_preprocess_http
docker build -t localhost:5001/loom/yolo11n-http:0.1.0 capability_services/yolo11n_http
```

Expected: tests pass and both builds exit 0.

- [ ] **Step 6: Commit locally**

Stage only the two capability-service directories and their tests, then commit `feat: add reproducible inspection capability services`.

### Task 4: Deterministic risk and aggregate capabilities

**Files:**
- Create: `benchmarks/composite_heterogeneous_scheduling/programs.py`
- Create: `benchmarks/composite_heterogeneous_scheduling/contracts.py`
- Create: `tests/benchmarks/test_composite_programs.py`

**Interfaces:**
- Consumes: YOLO response per image; then a list of 100 risk results.
- Produces: deterministic per-image risk records and one deterministic batch report.

- [ ] **Step 1: Write failing program execution tests**

Run each program with `ProcessJSONStdioV1Adapter`, not `exec` in the test. Assert literal low/high-risk examples, class totals, risk totals, duplicate detection and deterministic image-ID digest.

- [ ] **Step 2: Verify RED**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest tests/benchmarks/test_composite_programs.py -q
```

Expected: module import failure.

- [ ] **Step 3: Implement risk, aggregate and contract builders**

Keep executable programs as explicit source strings because Loom materializes them as `process:json_stdio/1` package bodies. Define exact JSON schemas and IO contract builders in a separate module.

- [ ] **Step 4: Run tests and commit locally**

Run the test from Step 2 and commit the two modules plus tests as `feat: add deterministic inspection analysis capabilities`.

### Task 5: Composite DAG runner and metric collector

**Files:**
- Create: `benchmarks/composite_heterogeneous_scheduling/run_composite.py`
- Create: `benchmarks/composite_heterogeneous_scheduling/metrics.py`
- Create: `benchmarks/composite_heterogeneous_scheduling/__init__.py`
- Create: `tests/benchmarks/test_composite_runner.py`
- Create: `tests/benchmarks/test_composite_metrics.py`

**Interfaces:**
- Consumes: 100 manifest entries, registered preprocess/YOLO packages, policy/topology labels.
- Produces: a Loom Run with 301 nodes, raw evidence JSON, metrics JSON and final task-output JSON.

- [ ] **Step 1: Write failing DAG-shape and metrics tests**

Assert that generated orchestration source creates 100 preprocess handles, then 100 YOLO nodes from preprocess results, then 100 risk nodes, then one aggregate node. Assert literal stage counts and latency calculations from a hand-built Run fixture.

- [ ] **Step 2: Verify RED**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest \
  tests/benchmarks/test_composite_runner.py \
  tests/benchmarks/test_composite_metrics.py -q
```

Expected: import failure.

- [ ] **Step 3: Implement Run construction**

Reuse the existing MCP helper style from `run_loom_yolo.py`. Materialize risk and aggregate process packages, bind reusable preprocess and YOLO service packages, create an orchestration package with `max_nodes=301` and `max_live_nodes=100`, then start and poll the Run.

- [ ] **Step 4: Implement evidence-based metrics**

Join dynamic nodes, accepted events and Attempts by node ID. Report Run wall time, per-stage count/mean/median/P95/max, per-Slave and per-package assignments, scheduler overhead, inference time, correctness checks, package/image digests and final content reference.

- [ ] **Step 5: Run tests and commit locally**

Run Step 2 tests plus existing benchmark tests. Commit as `bench: add 301-node composite inspection runner`.

### Task 6: Calibration and experiment matrix tooling

**Files:**
- Create: `benchmarks/composite_heterogeneous_scheduling/run_calibration.py`
- Create: `benchmarks/composite_heterogeneous_scheduling/run_matrix.py`
- Create: `benchmarks/composite_heterogeneous_scheduling/summarize.py`
- Create: `benchmarks/composite_heterogeneous_scheduling/README.md`
- Create: `tests/benchmarks/test_composite_calibration.py`
- Create: `tests/benchmarks/test_composite_summary.py`

**Interfaces:**
- Consumes: topology, policy, repeat count and output root.
- Produces: pinned calibration JSON, one raw JSON per Run and aggregate comparison tables.

- [ ] **Step 1: Write failing threshold and summary tests**

Use literal calibration samples to prove pass/fail for `A > B > C`, ratio ≥1.5 and coefficient of variation ≤0.30. Use mixed successful/failed Run fixtures to prove failures remain in success-rate denominators.

- [ ] **Step 2: Verify RED**

Run the two new test files and confirm missing-module failures.

- [ ] **Step 3: Implement calibration runner**

Create locality-constrained Loom Runs per capability and Slave, perform two warmups plus ten measured requests, and write configuration plus execution evidence. Exit non-zero when the design thresholds fail.

- [ ] **Step 4: Implement matrix runner and summary**

Run six policy/topology combinations in seeded randomized order, never overwrite a prior result, and aggregate success rate and latency distributions. Require explicit `--formal` to run 100 images; default to a 3-image smoke run.

- [ ] **Step 5: Run tests and commit locally**

Commit as `bench: automate heterogeneous calibration and strategy matrix`.

### Task 7: Docker deployment and live experiments

**Files:**
- Create: `benchmarks/results/test_results/composite_heterogeneous_scheduling/**`
- Create: `benchmarks/results/task_outputs/composite_heterogeneous_scheduling/**`
- Modify only if evidence requires: files implemented in Tasks 1–6, with a failing regression test first.

**Interfaces:**
- Consumes: built images, running Compose stack, 100-image local dataset.
- Produces: calibration evidence, smoke evidence, six formal experiment cells and final reports.

- [ ] **Step 1: Rebuild and start A+B+C topology**

Start with the feature-worktree source mounted into Observer, Driver and all Slaves. Confirm leases, ports, CPU limits, package registration and runtime descriptors.

- [ ] **Step 2: Register and activate service packages**

Upload immutable preprocess and YOLO manifests using actual image digests, activate both on A/B/C, and require all six activations to reach `ready`.

- [ ] **Step 3: Run pinned calibration**

Execute two warmups and ten measurements for preprocess and YOLO on each Slave. Stop if the calibrated YOLO medians fail the heterogeneity thresholds.

- [ ] **Step 4: Run 3-image smoke matrix**

Execute all three policies on A+B+C. Verify 10 dynamic nodes per Run, identical task-output digest, complete scheduling evidence and correct policy IDs.

- [ ] **Step 5: Run 100-image initial matrix**

Execute all six topology/policy cells three times, interleaving their order. Preserve failures and raw Run IDs. If runtime makes the full matrix impractical, complete at least one successful 100-image Run per cell and state the missing repetition count explicitly.

- [ ] **Step 6: Summarize and verify evidence**

Produce machine-readable aggregate JSON and Markdown tables. Cross-check totals against raw files and Observer Run records.

### Task 8: Final verification and report

**Files:**
- Create: `docs/composite-heterogeneous-scheduling-experiment-report.md`
- Update: `docs/heterogeneous-scheduling-experiment-full-guide.md`

**Interfaces:**
- Consumes: design, implementation commits, calibration and matrix results.
- Produces: a concise research-progress report and a reproducible execution guide.

- [ ] **Step 1: Run the complete relevant test suite**

```bash
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest \
  tests/driver/test_scheduling.py \
  tests/driver/test_dynamic_orchestration_runtime.py \
  tests/deploy \
  tests/capability_services \
  tests/benchmarks -q
```

- [ ] **Step 2: Validate Compose and artifact integrity**

Run `docker compose ... config --quiet`, validate all result JSON with `jq`, and compare reported aggregates against a fresh calculation from raw results.

- [ ] **Step 3: Write the report**

Document the research question, DAG, topology, calibration proof, algorithms, results, observed failures, code changes, evidence limits and next real-device experiment. Do not claim real heterogeneous hardware superiority.

- [ ] **Step 4: Review the branch without pushing**

Run `git diff --check`, inspect `git status`, list local commits relative to `origin/feat/pluggable-heterogeneous-scheduling`, and confirm no `git push` was executed.
