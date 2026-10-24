# meiva:note
# Process and runtime identity — what a process knows about itself, above `log`.
#
# Everything a process can know about itself without Settings, a database read,
# or a session backend: OS platform flags (`os_platform`), native birth facts
# (`kernel_identity`: PID plus kernel start ticks in an explicit boot scope), the commit a
# *running* process actually loaded (`loaded_commit`, as opposed to what the
# checkout is at right now) and the ordered code version derived from it
# (`code_version`), native admission and resource ownership scopes
# (`turn_identity`, `runtime_incarnation`), one bounded POSIX command's process
# group (`posix_command`), one exec child's process group (`exec_domain`), child-process environment primitives (`child_env`), strict
# process-observation value types (`evidence`), and the root supervisor's local
# control contract (`root_control`) and the stop-clock family (`stop_timing`). Moving the commit,
# platform and identity primitives here — rather than leaving them beside the sessions that use
# them — lets every native session host share them without import cycles. `log` sits below this
# package: it stamps the loaded commit through its structural `LoadedImage`, never importing
# `loaded_commit`, so the logger can be used here (`turn_identity`).
#
# Linux birth identity is PID plus kernel start ticks in an explicit boot scope.
# Wall-derived timestamps remain diagnostic. Serialized receipts retain all fields;
# only independent observations compare through `birth_key.process_birth_key`.
