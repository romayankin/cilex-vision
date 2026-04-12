# Cilex Vision ROI Calculator Template

This template helps commercial teams and customers estimate the business case for a deployment. It is intended for workshop use, not as a binding quote.

## How to Use This Template

1. capture the customer’s current operating baseline
2. estimate where automation reduces review time, incident loss, or manual effort
3. enter project costs using the customer’s preferred deployment model
4. compare annual benefit to annualized cost
5. calculate payback period and first-year ROI

## 1. Input Worksheet

### Site profile

| Input | Customer value | Notes |
|---|---|---|
| Number of sites |  | |
| Cameras per site |  | |
| Total cameras |  | |
| Operating hours per day |  | |
| Security or operations staff affected |  | |

### Current-state operating baseline

| Input | Customer value | Notes |
|---|---|---|
| Incidents requiring video review per month |  | |
| Average review time per incident (minutes) |  | |
| Average hourly labor cost |  | Include loaded labor cost if available |
| False alarms or low-value reviews per month |  | |
| Average time spent per false alarm (minutes) |  | |
| Average loss or cost per missed / late-response incident |  | Use customer estimate |
| Average response time today (minutes) |  | |
| Average evidence retrieval time today (minutes) |  | |

### Improvement assumptions

| Input | Customer value | Notes |
|---|---|---|
| Reduction in review time (%) |  | |
| Reduction in false-alarm handling time (%) |  | |
| Reduction in missed or late-response incidents (%) |  | |
| Reduction in manual patrol or monitoring hours (%) |  | Optional |
| Faster evidence retrieval (%) |  | Optional |

### Project cost inputs

| Input | Customer value | Notes |
|---|---|---|
| One-time deployment cost |  | Contact sales |
| Hardware or infrastructure cost |  | Customer-owned or partner-supplied |
| Annual software / service fee |  | Contact sales |
| Annual support cost |  | By support tier |
| Annual training / change-management cost |  | Optional |

## 2. Core Calculations

Use the following formulas.

### Labor savings from faster review

`Annual labor savings = incidents per month × review minutes × reduction % × hourly labor cost ÷ 60 × 12`

### Savings from fewer false-alarm reviews

`Annual false-alarm savings = false alarms per month × handling minutes × reduction % × hourly labor cost ÷ 60 × 12`

### Incident-loss reduction

`Annual incident-loss reduction = missed or late-response incidents per year × average loss per incident × reduction %`

### Optional patrol or monitoring savings

`Annual staffing efficiency gain = annual manual hours × hourly labor cost × reduction %`

### Total annual benefit

`Total annual benefit = labor savings + false-alarm savings + incident-loss reduction + staffing efficiency gain`

### Annualized cost

`Annualized cost = annual software or service fee + annual support + annual training + annualized hardware and deployment cost`

### Payback period

`Payback period (months) = one-time deployment cost ÷ (total annual benefit ÷ 12)`

### ROI

`ROI % = (total annual benefit - annualized cost) ÷ annualized cost × 100`

## 3. Output Summary

| Metric | Value |
|---|---|
| Total annual benefit |  |
| Annualized cost |  |
| Net annual value |  |
| Payback period (months) |  |
| First-year ROI |  |

## 4. Scenario Templates

These are workshop starters. Replace all values with customer figures.

### Scenario A: 10-camera retail site

| Input area | Example assumption |
|---|---|
| Main goal | Reduce manual incident review and speed up evidence retrieval |
| Review workload | Frequent short investigations across a small store estate |
| Highest-value drivers | Labor savings, faster dispute handling, reduced manual review time |
| Best-fit package discussion | Basic or Pro, depending on event and attribute needs |

### Scenario B: 50-camera campus

| Input area | Example assumption |
|---|---|
| Main goal | Improve cross-camera investigations and reduce operator overload |
| Review workload | Multiple buildings, perimeter zones, and recurring loitering or access incidents |
| Highest-value drivers | Faster response, fewer missed events, reduced investigation time |
| Best-fit package discussion | Pro or Enterprise |

### Scenario C: 100-camera enterprise estate

| Input area | Example assumption |
|---|---|
| Main goal | Standardize visibility and investigations across many sites |
| Review workload | Large number of cameras, multiple stakeholders, centralized monitoring |
| Highest-value drivers | Multi-site efficiency, evidence handling speed, reduced operational complexity |
| Best-fit package discussion | Enterprise |

## 5. Discovery Questions for Sales Workshops

- Which incidents currently take the most time to investigate?
- How many staff hours are spent reviewing video every week?
- What is the average business cost of a delayed or missed response?
- How many sites need to be managed centrally?
- Is the customer trying to improve security response, operations efficiency, or both?
- Which outcome matters most: faster review, fewer incidents, lower labor cost, or better multi-site control?

## Commercial Note

This ROI template is a planning aid. Final commercial terms, deployment scope, and solution sizing should be confirmed with sales and solution engineering.
