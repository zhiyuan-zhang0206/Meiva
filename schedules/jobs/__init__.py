# meiva:note
# What the built-in schedules do: the loop that claims slots, the directory calls that find or
# wake an agent, and the body each schedule runs when its slot is claimed. Decisions live in
# `schedules/plans`; these modules read the world, call the plan and execute its answer.
# meiva:link schedules/README.md
