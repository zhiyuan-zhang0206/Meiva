# Recovery after a schedule manager restart

Declarative fires have durable run rows and run-specific process claims. A restarted
manager observes those claims and settles completed, missing, or timed-out process
groups before claiming another fire. Recurring schedules advance after settlement;
`at` schedules finish once. The manager never creates a resident PTY session.

A machine with multiple active units elects one home through `machine_units`,
preferring the gateway unit and then the lowest home path. Only that home reconciles
schedule runs, which prevents co-located managers from double-firing schedules.
