# Evaluation calibration fixture

A balanced case set (12 expected-pass, 12 expected-fail) plus the harness that scores a judge
against it. Use it before trusting any "agreement" number: on a random run sample almost every
run passes, so an always-pass stub outscores a real judge.

Run the baselines (no model call):

```bash
.venv/bin/python meiva_builtins/skills/platform/meiva-self-evolution/scripts/calibrate.py \
  --cases meiva_builtins/skills/platform/meiva-self-evolution/tests/fixtures/eval_calibration/cases.json \
  --judge always-pass
```

Score a real judge by passing `--judge my_pkg.judge:evaluate`, where the callable takes one case
dict (`case_id`, `task`, `evidence`, `telemetry`, `expected`) and returns `pass`, `fail` or
`uncertain`. `uncertain` counts as neither correct nor caught, so abstaining cannot inflate the
score. Exit code is 0 only when accuracy strictly beats both baselines.

Cases are synthetic: they carry no production trace, user text or agent identity. `synthesize_failure`
in the harness turns a passing packet into a promise-only failure, which is how the fail half was
built and how new cases should be added.
