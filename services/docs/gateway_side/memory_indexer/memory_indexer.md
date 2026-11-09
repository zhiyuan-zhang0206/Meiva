# Memory Indexer

## What is it

Overview of the Memory Indexer subsystem.

## Subconcepts

- [Memory Indexer](memory-indexer.md)
- [Backend Provisioning](backend-provisioning.md)

## Process composition

The main retains its existing configuration boot owner and captures one loaded
image. Its lazy log database uses that owner's live dial slice and process gate;
the indexer's work handle and health use the same factory and image. The search
server adds no database startup requirement. Before hard exit each daemon stops
its owned pipeline with a two-second bound; an unfinished receipt is reported,
and cleanup failure retains a failing exit. The standalone reconciliation tool
captures its gate only after confirmation and a nonempty query pool.
