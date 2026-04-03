# Human Review Checklist: DESIGN Tasks

Run `.agents/review.sh {task-id}` first for automated checks.
Then verify:

## Completeness
- [ ] Does the spec cover ALL cases from taxonomy.md? (Every object class, every event type)
- [ ] Are field types correct? (No string where int needed, no missing required fields)
- [ ] Are enums complete? (No missing values that downstream tasks will need)

## Implementability
- [ ] Could a Dev agent implement this WITHOUT asking you questions?
  (The "hand it to a stranger" test)
- [ ] Are acceptance criteria machine-verifiable?
  (Not "should be fast" but "p99 latency < 200ms measured by Prometheus histogram")
- [ ] Are examples provided for ambiguous cases?

## Consistency
- [ ] Does this spec contradict any existing spec in docs/?
- [ ] Do Protobuf field names match what's used elsewhere?
- [ ] Do topic names match docs/kafka-contract.md?

## Evolution Safety
- [ ] Are Protobuf changes backward-compatible? (No removed fields, no changed field numbers)
- [ ] Can existing data be migrated if the schema changes?
