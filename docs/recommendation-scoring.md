# Recommendation scoring

Bo treats Boston University Charles River Campus as its home base. The score is
an internal, explainable ranking signal; it is not presented to readers as an
objective rating of an event.

## Version 1.0

Each eligible event receives up to 100 points:

| Component | Points | Purpose |
| --- | ---: | --- |
| Proximity | 30 | Prefer BU, Boston, and nearby transit-friendly cities |
| Data quality | 20 | Reward reliable date, time, venue, link, description, and price |
| Interest | 20 | Let distinctive or seasonal events overcome some distance |
| Affordability | 10 | Prefer free and lower-cost options |
| Timeliness | 10 | Prioritize events readers can plan now |
| Source confidence | 10 | Distinguish official calendars from aggregators |

Exact coordinates are used when the source supplies them. Otherwise the scorer
uses transparent city and neighborhood tiers. BU, Kenmore, Fenway, Allston,
Brighton, Back Bay, Brookline, and the Charles River area receive the strongest
proximity preference. Boston remains preferred, followed by inner Greater
Boston; Natick and other outer locations need stronger interest value to rank
above a nearby event.

## Semantic ranking by Bo

Version 1.0 remains a deterministic baseline and safety layer. It filters
unavailable or invalid events and supplies factual distance and data-quality
evidence, but it no longer decides the final editorial ranking.

One batched LLM call ranks all eligible candidates together using Bo's versioned
persona and reviewed long-term memory. It scores six dimensions totaling 100:

| AI component | Points | Purpose |
| --- | ---: | --- |
| Leisure appeal | 25 | How compelling the experience is for a leisure outing |
| Local significance | 25 | Cultural or community importance around Greater Boston |
| Rarity | 20 | Annual, unusual, seasonal, or hard-to-repeat value |
| Value | 10 | Price relative to the experience |
| Proximity fit | 10 | Convenience from the BU/Boston home context |
| Information confidence | 10 | How well the source supports a recommendation |

Distance is a preference, not a veto, and there is no fixed local-versus-distant
quota. Semantic ranking lets an event such as the Revere International Sand
Sculpting Festival outrank routine nearby options because it is rare and locally
significant, without relying on a brittle keyword bonus.

The campaign decision trail stores the deterministic baseline, the six AI
components, bilingual reasons, significance signals, destination judgment,
`base_score_rank`, `final_score_rank`, published `final_selection_rank`, model,
prompt version, token usage, and memory version.

Canceled, postponed, rescheduled, off-sale, and sold-out events receive an
ineligible status and a zero recommendation score. They remain in immutable
event history so status changes can be analyzed.

Each social campaign stores the full eligible candidate ranking, whether each
candidate was selected, its total and component scores, and its final social
ranking score. This creates a decision trail for later comparison with manual
feedback or Threads insights. Weight changes require a new schema version so
historical results remain interpretable.
