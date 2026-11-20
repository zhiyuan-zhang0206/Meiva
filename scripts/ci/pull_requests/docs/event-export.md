# CI Sampler Event Writer

Standalone `pr_flow_export.py` captures its source image after argument
validation and before remote collection. Only actual emission constructs its
writer through `export_process.owned_event_pipeline`: one CodeVersion and
nonexempt ProcessDbGate supply its live database factory, and the home the
entry's `__main__` block resolved once locates the cluster it dials.
A missing loaded SHA remains unknown; no dial captures the current checkout.

The writer receives a finite two-second stop/join budget. An unfinished receipt
is reported when the writer or exporter has not finished; this is not a drain guarantee;
the telemetry binding retains the actual pipeline for final process draining.
An export error stays primary if writer cleanup also fails, with the original
worker error retained by the pipeline for repeated collection.

PR-flow cache and snapshot replacement use the shared atomic writer. Each new
file is private to the Meiva user (0600); interrupted or failed replacement leaves
no partial report. Consumers run as that same user. This tightens the earlier
umask-dependent temporary-file mode without changing the JSON representation.

GitHub command transport is required at the sampler entry and fetch boundary.
The script root and the watch schedules supply standard-library subprocess.run, retaining
60-second command timeouts, inherited environment/cwd and the sampler's rc policy.
PR-flow timeline exhaustion is reported as truncation. Collection, cache writes
and event emission run their real owners.

`runs_api.py` owns one read-only `gh api` call with bounded transient-error
retries and request accounting. A watch schedule's direct workflow query
(`schedules/jobs/watch/api_read.py`) calls it with the standard process
transport and the required Timers port, without loading clock configuration
and without changing the slot callback shape.
