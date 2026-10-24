# meiva:note
# What the data-plane maintenance stop decides, as values.
#
# Each module takes the facts `maintenance_stop` read (the captured services, the process
# table, the pooler's console) and returns the order to stop in, the refusal or the line
# to report. `maintenance_stop.py` executes what these return.
