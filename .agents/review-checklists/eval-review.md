# Human Review Checklist: EVAL Tasks

## Results Validity
- [ ] Were all candidates evaluated on the SAME dataset, hardware, and conditions?
- [ ] Are metrics calculated correctly? (Verify formula, not just the number)
- [ ] Is the MLflow run reproducible? (Re-running produces same results)

## Comparison Fairness
- [ ] Same preprocessing for all candidates?
- [ ] Same batch size and precision (FP16) for all?
- [ ] Latency measured under same GPU load conditions?

## Decision Quality
- [ ] Does the recommendation follow from the data?
- [ ] Are trade-offs clearly stated?
- [ ] Would you make the same choice looking at the numbers?
