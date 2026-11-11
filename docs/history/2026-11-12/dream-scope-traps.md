# Dream scope-trap comparison cost

Dream repeatedly parsed every note for each pair. Parse once and use an inverted
index with globally ordered Jaccard prefixes for threshold comparisons. Pool
coverage uses a full token index because its exact sub-threshold maximum appears
in the output. Heading matches bypass token-length pruning.

Keep the original similarity function as the brute-force compatibility oracle.
A deterministic 3900-note fixture guards sparse matching with shared vocabulary;
dense output can itself require quadratic space and time.
