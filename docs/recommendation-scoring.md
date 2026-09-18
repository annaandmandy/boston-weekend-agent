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

## Destination-worthy boost without quotas

Distance is a preference, not a veto, and there is no fixed local-versus-distant
quota. Festivals, sand-sculpture events, parades, fireworks, carnivals,
regattas, cultural festivals, and open-studios events receive a transparent
destination-worthy adjustment before all candidates are freely ranked together.
The final five may therefore contain any mix of local and outer Greater Boston
events. This keeps routine recommendations close to BU without hiding events
such as the Revere International Sand Sculpting Festival.

The campaign decision trail stores the base recommendation, each social
adjustment, `base_score_rank`, adjusted `final_score_rank`, the published
`final_selection_rank`, and `selection_lane=destination_boost` where applicable.

Canceled, postponed, rescheduled, off-sale, and sold-out events receive an
ineligible status and a zero recommendation score. They remain in immutable
event history so status changes can be analyzed.

Each social campaign stores the full eligible candidate ranking, whether each
candidate was selected, its total and component scores, and its final social
ranking score. This creates a decision trail for later comparison with manual
feedback or Threads insights. Weight changes require a new schema version so
historical results remain interpretable.
