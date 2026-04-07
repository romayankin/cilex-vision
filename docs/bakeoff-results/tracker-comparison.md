---
version: "1.0.0"
status: P1-E02
created_by: eval-agent
date: "2026-04-07"
---

# Tracker Bake-Off Comparison

This is a published-benchmark proxy comparison for P1-E02. The full tracker
protocol in `docs/bake-off-protocol.md` could not be executed here because this
environment does not have a populated `data/eval/mot/` set, a live Triton
stack, or MOT-format pilot ground truth. The detector selected in
`docs/bakeoff-results/detector-comparison.md` is still `YOLOv8-L`, but the
published tracker numbers below come from each method's official MOT17 private
detection submission rather than from live `YOLOv8-L` outputs on the pilot
clips.

## Method

The comparison still follows the tracker decision rule from
`docs/bake-off-protocol.md`:

1. highest `IDF1`
2. highest `MOTA`
3. lowest `ID_switches`
4. lowest `fragmentation`

Because the protocol does not define a composite score for trackers, this
report uses the ordered metrics directly. The published proxy is narrower than
the eventual live bake-off in two ways:

- the benchmark is MOT17 test with private detections, not the pilot's
  `YOLOv8-L` detections
- the benchmark is still pedestrian-centric, while the pilot architecture must
  ultimately support the broader taxonomy from `docs/taxonomy.md`

That means this report is a ranking proxy, not a substitute for the real
tracker bake-off run once `data/eval/mot/` exists.

## Comparison

| Rank | Candidate | MOTA | IDF1 | ID Switches | Fragmentation | Mostly Tracked % | Mostly Lost % | Published FPS |
|------|-----------|------|------|-------------|---------------|------------------|---------------|---------------|
| 1 | BoT-SORT | 80.5 | 80.2 | 1,212 | 1,803 | 54.4 | 16.2 | 6.8 |
| 2 | ByteTrack | 80.3 | 77.3 | 2,196 | 2,277 | 53.2 | 14.5 | 29.6 |

Key readout:

- `IDF1`: BoT-SORT leads by `+2.9`, which is material on the protocol's
  primary metric.
- `MOTA`: BoT-SORT still leads, but only by `+0.2`.
- `ID_switches`: BoT-SORT reduces switches by `984` versus ByteTrack, about
  `44.8%` fewer.
- `fragmentation`: BoT-SORT reduces fragmentation by `474`, about `20.8%`
  fewer.

Generated artifacts:

- `docs/bakeoff-results/tracker/idf1_ranking.svg`
- `docs/bakeoff-results/tracker/primary_metrics.svg`
- `docs/bakeoff-results/tracker/association_errors.svg`

## Metric Analysis

- `IDF1`: This is the decisive metric here. BoT-SORT's appearance-assisted
  association materially improves identity continuity over ByteTrack in the
  official benchmark.
- `MOTA`: The raw detection/tracking accuracy difference is small. If MOTA were
  the only metric, this would not justify extra integration complexity.
- `ID_switches`: This is where BoT-SORT separates most clearly. Fewer identity
  swaps directly align with the pilot's downstream needs for stable local
  tracklets and cleaner Re-ID handoff.
- `fragmentation`: BoT-SORT also produces fewer broken trajectories, which
  reduces track churn and makes later event logic less noisy.
- `throughput`: ByteTrack remains the simpler and much faster baseline in the
  published sources. That does not override the protocol ranking, but it does
  matter operationally: BoT-SORT's published `6.8 FPS` figure is far below
  ByteTrack's `29.6 FPS`, so the real pilot run still needs a measured latency
  check on the target stack.

## Recommendation

**Recommend `BoT-SORT` as the tracker winner for the live bake-off follow-on.**

Rationale:

- It wins all four protocol metrics on the official MOT17 private-detection
  leaderboard: `IDF1`, `MOTA`, `ID_switches`, and `fragmentation`.
- The largest separation is on identity quality, not just on marginal accuracy.
  That is the more important signal for a multi-camera pipeline where local
  identity stability feeds later Re-ID and event logic.
- The safe-default fallback is not needed here because the published ordering is
  not ambiguous. BoT-SORT is the clear proxy winner.

Operational caveat:

- `ByteTrack` is still the safer currently implemented baseline in this repo,
  and it remains the protocol fallback if the live `YOLOv8-L` / pilot-clip
  bake-off later becomes inconclusive.
- Before promoting BoT-SORT into production, the team still needs one real
  tracker bake-off run against pilot MOT ground truth generated from
  `scripts/annotation/setup_cvat_projects.py` outputs, using the chosen
  `YOLOv8-L` detector outputs rather than the papers' private detectors.

## Source Notes

- Official MOT17 private-detection leaderboard rows for both candidates:
  [motchallenge.net/results/MOT17/?det=Private](https://motchallenge.net/results/MOT17/?det=Private)
- ByteTrack official repo / README:
  [github.com/FoundationVision/ByteTrack](https://github.com/FoundationVision/ByteTrack)
- ByteTrack official README metrics block:
  [raw.githubusercontent.com/FoundationVision/ByteTrack/main/README.md](https://raw.githubusercontent.com/FoundationVision/ByteTrack/main/README.md)
- BoT-SORT official repo / README:
  [github.com/NirAharon/BoT-SORT](https://github.com/NirAharon/BoT-SORT)
- BoT-SORT official README metrics block:
  [raw.githubusercontent.com/NirAharon/BoT-SORT/main/README.md](https://raw.githubusercontent.com/NirAharon/BoT-SORT/main/README.md)
