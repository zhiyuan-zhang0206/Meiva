---
type: doc
title: "Computer MCP process composition"
description: "The computer executable entry shares one captured image gate and explicit database factory between logging and action audits."
tags:
- services
- computer-use
---

# Computer MCP process composition

`services.desktop.computer.mcp_daemon.main` captures one `LoadedCommit` and
supplies that same image to logging and the `CodeVersion` behind its
`ProcessDbGate`. An unknown image stays unknown rather than borrowing a later
checkout's version. The validated machine reader retains the existing live
settings owner.

One explicit database factory serves the logging pipeline and
`run(sock, database=...)`. Each factory call reads the current settings and
supplies the same gate; the daemon passes the resulting handle to action and
session audits. Logging construction retains its lazy database dial.

`run` keeps the existing computer-use configuration, optional socket argument,
single-instance guard and handler identity. Its shutdown still stops the loop
watchdog, closes the listener, joins the watchdog within one second, then drains
the tracked clients within the configured bound, and unlinks the socket. Errors keep their existing propagation and the
lock-free Unix `ping` payload remains unchanged.

Stopping requests the watchdog's exit and waits for the actual thread before returning,
including when the serving task is cancelled. Failure to join within one second triggers
the existing process dump-and-exit boundary. Real Unix socket tests cover SIGTERM and cancellation.
