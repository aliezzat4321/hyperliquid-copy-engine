# Issue #120 protected workflow repair

`docs/ai-team/AUTONOMOUS_TEAM.md` permits workflow changes but requires non-automatic
merge. However, Issue #120's explicit autonomous-builder boundary prohibits this builder
from editing `.github/workflows/**`, so the manager must make the following exact
protected-path change after exact-SHA independent review:

```diff
diff --git a/.github/workflows/hyperliquid-emergency-storage-reclaim.yml b/.github/workflows/hyperliquid-emergency-storage-reclaim.yml
@@
-            --target-used-pct 92
+            --target-used-pct 75
@@
           echo 'REAL_TRADING_CHANGE=NO'

+      - name: Preflight exact reviewed manifest before capture quiesce
+        shell: bash
+        env:
+          REVIEWED_CODE_SHA: ${{ inputs.reviewed_code_sha }}
+          REVIEWED_MANIFEST_SHA256: ${{ inputs.reviewed_manifest_sha256 }}
+        run: |
+          set -euo pipefail
+          MANIFEST="$REVIEW_ROOT/$REVIEWED_CODE_SHA.json"
+          python3 scripts/storage_retention_apply.py \
+            --manifest "$MANIFEST" \
+            --expected-manifest-sha256 "$REVIEWED_MANIFEST_SHA256" \
+            --max-manifest-age-minutes 360 \
+            --target-used-pct 75
+          echo 'APPLY_PREFLIGHT=PASSED'
+
       - name: Quiesce Hyperliquid capture immediately before apply
@@
-            --target-used-pct 92
+            --target-used-pct 75
```

Both occurrences are required: the first is the reviewed-manifest dry-run and the
second is destructive apply. `75` is the executable's default, is inside its enforced
`[70, 92]` inclusive validation range, and satisfies Issue #120's preferred post-cleanup
target below 75--80%.

The inserted step runs immediately before capture quiescence and invokes the same
validation path without `--apply`. Normal step ordering makes quiescence depend on its
success. This prevents argument or manifest validation failures from leaving capture
stopped before any reclaim attempt.

After the protected edit, run:

```text
python -m pytest tests/test_storage_controller.py tests/test_storage_retention_apply.py
python -m ruff check scripts/storage_controller.py scripts/storage_retention_apply.py \
  tests/test_storage_controller.py tests/test_storage_retention_apply.py
```

Do not dispatch apply until the resulting exact commit SHA has independent Claude Opus
review and the reviewed manifest SHA-256 is supplied. This change does not authorize an
apply, resume capture, touch real trading, or alter any live-trading gate.
