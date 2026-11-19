# Vendored DevArch Framework package

`archaeology/` is copied from KyaniteLabs/devarch-framework @ `b44430e198e45bfaee1edb2f3473f7d50ce17bc2` (v0.4.1).

Omitted from the vendor copy (not used by this Hermes wrap):
- `mcp_server/` (optional `mcp` extra)
- `templates/` and large `visualization/*.html` dashboards (HTML comment scanner caution; wrap uses markdown report + `_markdown_to_html`)

The framework at the repository root owns shared extraction and report behavior. Keep fixes specific to the native Hermes wrapper in the wrapper or in the recorded vendor patch. When a framework fix affects both entry points, update the root framework first and keep the native copy in sync.

From a checkout with the upstream commit in its Git history, verify the committed copy:

```bash
bash scripts/devarch-vendor.sh check
```

The check reconstructs the tree from the plugin's source pin and applies [the recorded patch](../../scripts/devarch-vendor.patch). CI compares that result to the checked-in vendor tree.

To refresh, update `PINNED_SHA` in the plugin and render to a new scratch directory before touching the existing vendor tree:

```bash
bash scripts/devarch-vendor.sh render /tmp/devarch-vendor-candidate
```

If the patch conflicts, the command leaves the new source tree in the scratch directory and exits with an error. Reconcile the patch there and review the diff before updating the vendored files. After intentional edits or a refresh, record the revised patch and rerun the check:

```bash
vendor_patch=$(mktemp)
bash scripts/devarch-vendor.sh record > "$vendor_patch" && mv "$vendor_patch" scripts/devarch-vendor.patch
bash scripts/devarch-vendor.sh check
```

## Plugin-local patches (KLH-328 Kai review)

Applied on top of the pinned vendor copy for the Hermes wrap (kept in sync with matching framework `archaeology/` fixes where noted):

- `extractors/git.py` — `GIT_NO_LAZY_FETCH=1` on all git invocations; `incomplete_object_reason()` refuses promisor/partial/missing-object repos (local-only contract).
- `report.py` — include measured `detected-signals.json` + `gap-clusters.json`; honest era/cluster semantics; curated `commit-eras.json` preserved; `verify_mining_inputs` + `verify_derived_stage`; `verify_analyses(require=…)` only when analysis artifacts exist.
- `provenance.py`, `metrics.py` — present derived metrics/signals/clusters require both the current generation and a content-hash sidecar (also synchronized to root framework). Optional absence does not exempt a present artifact. Signal detection may skip the two signal files it is about to rewrite; export never skips them.
- `extractors/git.py` — positive commit cap for both raw/stats extraction, disabled transports and optional Git locks; full ref coverage remains separately reported.
- `report.py`, `visualization/dashboard.py`, `visualization/agent_benchmark.py` — package-relative imports support the isolated `_devarch_vendor` namespace. The wrapper never prefers an arbitrary installed framework.
- Wrapper project transactions serialize full operations across threads/processes; complete pipelines retain the same lock across nested stages.

After a refresh, run the framework and plugin tests, including the real Hermes dependency install and demo in the DevArch Hermes workflow. Review the mining, concurrency and evidence integrity tests along with the patch.
