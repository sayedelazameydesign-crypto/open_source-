# Evaluation harness

os_evals runs a suite against a real model through an OpenAI-compatible gateway.
The builtin suite is a connectivity check, not a quality metric, and prints that
warning so nobody quotes two passing cases as accuracy.
