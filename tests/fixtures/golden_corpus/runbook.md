# Deployment runbook

## Target hosts
The build farm runs on Debian 12 in eu-west-1. Staging is 10.0.0.7 and must never
be reached from the browser tools. Production hosts are behind the load balancer.

## Rollback
Rollback is `make rollback TAG=<prev>`. The ledger keeps the outcome of every run,
so a rollback is justified by the recorded failure, not by memory.
