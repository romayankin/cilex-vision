# Human Review Checklist: DEV Tasks

Run `.agents/review.sh {task-id}` first for automated checks.
Then verify these items that automation CANNOT check:

## Logic Correctness
- [ ] Does the service actually do what the spec says? (Read the prompt, then read main.py)
- [ ] Are edge cases handled? (What happens with empty input? Malformed data? Timeout?)
- [ ] Is the error handling appropriate? (Retry vs fail-fast vs skip-and-log)

## Spec Conformance
- [ ] Do Kafka topic names match docs/kafka-contract.md exactly?
- [ ] Do Protobuf message types match proto/*.proto exactly?
- [ ] Do DB table/column names match services/db/models.py exactly?
- [ ] Are the three timestamps (source_capture_ts, edge_receive_ts, core_ingest_ts) present?

## Performance
- [ ] No blocking I/O in async code (no `time.sleep()`, no sync DB calls)
- [ ] Batching used where appropriate (not one-message-at-a-time processing)
- [ ] Connection pooling for DB, Kafka, NATS (not connect-per-request)

## Security
- [ ] No hardcoded credentials
- [ ] Audit logging on sensitive operations (if touching user data)
- [ ] Input validation on API endpoints (if applicable)

## Test Quality
- [ ] Tests cover the HAPPY PATH (normal operation)
- [ ] Tests cover ERROR PATHS (what breaks and how)
- [ ] Tests cover EDGE CASES (empty, null, too large, duplicate)
- [ ] Tests are deterministic (no random failures)
