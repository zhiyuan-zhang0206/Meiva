# Scheduled-only recovery for release-gate gaps

Task #6817 reported three arrivals against four expected in its 24-hour window.
Read-only GitHub and normal-round evidence agreed; the missing arrival was not
caused by a local read gap. Separately, task #6822 found that a decision with no
new card also closed an existing one. A manual/PR run becoming busy or a channel
catching up therefore counted as scheduled recovery without a scheduled arrival.
No production instance of that false closure was demonstrated.

The decision now distinguishes waiting, opening and scheduled recovery. An open
gap ends only when its recorded scheduled creation timestamp advances; rerunning
the same run does not advance it. A recorded no-run gap ends on its first verified
arrival. Busy or zero uncertified commits still suppress a new card, while an
existing gap waits. Arrival and result are independent: a new red or queued run
restores the schedule just as a new green run does.

The reader filters the requested workflow/default branch by event=schedule and
uses a timestamp-bounded 24-hour search anchored to the newest arrival. Pagination
and reported totals establish coverage; mixed-event truncation, missing pages,
changed totals and GitHub's filtered-search cap remain unreadable. The published
100-row page/1,000-result search boundaries come from the GitHub REST contract,
not from the expected four daily arrivals. This permits explicit unknown instead
of turning a short list into proof of recovery.

The nine-hour threshold and six-hour cron are unchanged. The change performs no
workflow dispatch, certification, release, deployment or production state repair.
Historical metadata and isolated callbacks exercise recovery and waiting; normal
scheduled recovery of #6817 remains independent of this change's deployment.
