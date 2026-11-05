# Task row clock ownership

Task row conversion now receives the clock already owned by the SDK context or
gateway application. The former conversion read global Settings, so its timezone
and weekday policy could differ from the owner that created or read the task.

The earlier debt exit proposed adding a clock field to Task. Inspection showed
that timestamp conversion happens in task_from_row before Task validation, so
passing the existing clock through this conversion and the creation transaction
keeps the serialized model unchanged. Reading Settings in the caller would only
move the same acquisition; this change uses the clocks those callers already own.

The bounded file-lock timer debt remains separate. Its low-level module cannot
import base.clock without closing the existing clock -> config -> native_process
cycle, and several callers have no timer capability to pass. Removing its private
wrapper alone would not complete that ownership change.
