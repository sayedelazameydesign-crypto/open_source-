# Sandboxed execution

python_execute runs in a subprocess with rlimits, a wall-clock timeout, and a
network namespace when unshare is available. When unshare is refused the result is
reported as unverified rather than claimed as isolated.
