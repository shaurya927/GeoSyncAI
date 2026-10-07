# Security and traffic hardening — 7 October 2026

Preserve user datasets, volumes and offline drafts. Add controls to the existing
web application and verify the real API, browser and Compose deployment.

1. Bound traffic before parsing: trusted proxy identity, per-client/account limits,
   Redis-backed atomic counters, bounded concurrency, request size/time limits,
   request identifiers and redacted metrics. Add independent Nginx limits.
2. Require complete, scoped JWT claims; revoke tokens on connected logout and
   invalidate all sessions after password/account changes. Upgrade existing password
   hashes on login and provide real administration/provisioning controls.
3. Restrict browser origins and hosts, protect responses with security headers/CSP,
   bind demo ports to loopback, run API/worker as non-root and add production guards.
4. Strengthen archive/input and queued-job limits. Fix reported dependency advisories.
5. Add adversarial regression tests and a production browser security workflow;
   rerun existing geospatial/offline journeys and full service-stack CI. Update source
   package and document operator settings, measured traffic limits and remaining risks.

This work does not claim protection against every attack or provide a hosted DDoS
service. Public deployment still requires TLS, maintained infrastructure and capacity
testing on the intended host. Production mode must refuse weak demo credentials.
