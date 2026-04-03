# Human Review Checklist: OPS Tasks

Run `.agents/review.sh {task-id}` first for automated checks.
Then verify:

## Idempotency
- [ ] Run the playbook/compose twice — second run changes nothing?

## Alert Quality
- [ ] Do alert thresholds match NFRs in docs/taxonomy.md?
- [ ] Would you be woken at 3 AM by these alerts? (No too-sensitive alerts)
- [ ] Would a real outage be caught? (No missing critical alerts)

## Security
- [ ] No hardcoded passwords, tokens, or keys
- [ ] TLS/mTLS configured per docs/security-design.md
- [ ] Secrets use env vars or secret management, not config files

## Completeness
- [ ] All services that have Dockerfiles are in docker-compose
- [ ] Health checks configured for every container
- [ ] Volumes/data persistence configured (nothing lost on restart)
