# Dataset Coverage Report

Generated: (run `dataset_analysis.py` to populate)
Source manifest: `data/multi-site/unified-manifest.json`

## Overview

- **Total samples:** —
- **Sites:** —
- **Cameras:** —
- **Object classes with data:** —
- **Gap threshold:** 100 samples per class per site

## Class Distribution (Total)

| Class | Count | % |
|-------|------:|--:|
| person | — | — |
| car | — | — |
| truck | — | — |
| bus | — | — |
| bicycle | — | — |
| motorcycle | — | — |
| animal | — | — |

## Per-Site Class Distribution

| Class | site-alpha | site-beta | site-gamma |
|-------|------:|------:|------:|
| person | — | — | — |
| car | — | — | — |
| truck | — | — | — |
| bus | — | — | — |
| bicycle | — | — | — |
| motorcycle | — | — | — |
| animal | — | — | — |

## Condition Coverage

### Lighting

| Value | Count |
|-------|------:|
| day | — |
| night | — |
| mixed | — |

### Weather

| Value | Count |
|-------|------:|
| indoor | — |
| outdoor-clear | — |
| outdoor-rain | — |

## Camera Model Coverage

| Model | Samples |
|-------|--------:|
| — | — |

## Camera Coverage by Site

| Site | Cameras | Samples |
|------|--------:|--------:|
| — | — | — |

## Gap Analysis

Run `dataset_analysis.py` to identify classes with fewer than the threshold
number of samples at any site. Common gaps in multi-site deployments:

- **animal**: Rare in urban/indoor sites
- **bicycle/motorcycle**: Seasonal and location-dependent
- **truck/bus**: Low frequency at small sites

## Split Statistics

Run `balanced_split.py` to generate train/val/test splits. The split summary
will show per-site and per-class distribution across splits.

| Split | Items | Proportion | Sites | Cameras |
|-------|------:|------------|------:|--------:|
| train | — | 70% | — | — |
| val | — | 15% | — | — |
| test | — | 15% | — | — |

## Recommendations

- Schedule targeted annotation sessions for underrepresented class/site combinations
- Review condition balance before training (day/night, weather diversity)
- Ensure balanced splits preserve site and condition proportionality
- Version all datasets with DVC before use in training pipelines
- Re-run this report after each annotation batch to track progress toward coverage goals
