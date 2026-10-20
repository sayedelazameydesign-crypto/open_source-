# Egress and SSRF

Every outbound fetch passes the SSRF floor before the request and again on every
redirect hop. Loopback, RFC1918, link-local and the cloud metadata endpoint are
refused for agent-driven fetches. Operator-configured telemetry is the one place
where loopback is allowed, and only because the destination is configuration.
