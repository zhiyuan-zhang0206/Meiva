# Additional agent fixture cleanup

A test that directly creates an additional agent owns its termination before
the function-scoped gateway closes. `tests/e2e/lifecycle/test_fork_identity.py`
cannot rely on `spawned_agent` to clean up its extra fork; a survivor would keep
DB connections and compete with the next test after gateway teardown.
