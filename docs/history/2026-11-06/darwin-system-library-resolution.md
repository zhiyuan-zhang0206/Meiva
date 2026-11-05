# Darwin system library resolution

Memory pressure, process footprint and native boot identity ask for Darwin system
facilities. Their libc and libproc providers are fixed operating-system ABIs;
searching the process environment for alternatives adds a choice these probes
should not make. The probes now use the absolute system install names that dyld
resolves from its shared cache, rather than handing a library search to callers.

Both libraries loaded on the development Mac, and real pressure, current-process
footprint and boot UUID reads succeeded. Linux paths are unchanged. This removes
three environment reads with no new root declaration, allowance or rule change.
