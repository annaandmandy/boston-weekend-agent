# Greater Boston event sources

The collector treats Greater Boston as a travel radius, not a fixed city list.
Ticketmaster searches within 25 miles of downtown Boston, while municipal and
regional calendars fill gaps for festivals, markets, outdoor programs, library
events, and neighborhood celebrations.

## Active sources

| Source | Coverage | Adapter |
| --- | --- | --- |
| Ticketmaster Discovery API | Venues within 25 miles of Boston | JSON API using `geoPoint` and `radius` |
| Boston.gov | Boston public events | Official RSS |
| Cambridge Arts | Cambridge arts events | Official iCalendar |
| Malden Main Calendar | Malden community events | Official CivicPlus iCalendar |
| Natick Community Events | Natick community, recreation, and farm events | Official CivicPlus iCalendar |
| Brookline Community Calendar | Brookline non-board events | Official CivicPlus iCalendar |
| City of Revere | Revere community events, including beach festivals | Official calendar HTML |
| Discover Quincy | Quincy city, visitor, and library events | City visitor calendar HTML |

All sources are isolated: one provider failure is recorded in the snapshot and
does not prevent other providers from producing a report. Events are normalized,
government meetings are excluded, and duplicate title/date pairs retain the
record with the most useful details.

## Confirmed candidates

These are useful additions, but should be integrated only after their public
feed or page structure is covered by a fixture test.

| Source | Useful coverage | Integration note |
| --- | --- | --- |
| Somerville City Calendar | Festivals, farmers markets, arts, libraries | Official HTML calendar |
| Arlington Community Calendar | Town Day, concerts, recreation, arts | Official Granicus calendar |
| Watertown City Calendar | Community events and farmers market | Official iCalendar available |
| Lexington Community Calendar | Celebrations, tourism, recreation | Official CivicPlus iCalendar |
| Newton city and cultural calendars | Village days, markets, museums, festivals | Official Granicus calendars |
| Medford Events Calendar | Community and library events | Official RSS; meeting filtering required |
| Everett Events Calendar | City celebrations and recreation | Validate the WordPress calendar feed |
| Chelsea Recreation and Library | Community celebrations and programs | No single complete city feed |
| Waltham annual events | Festivals, concerts, Common events | Official annual calendar HTML |
| Belmont Community Events | Local community events | Official CivicPlus iCalendar |

## Regional candidates

Regional sources complement city calendars and should use distance filtering
before events enter the report:

- Massachusetts DCR park programs: hikes, tours, nature programs, beaches, and
  state parks such as Revere Beach, Blue Hills, and Walden Pond.
- The Trustees: farms, gardens, historic properties, outdoor arts, and family
  programs.
- Mass Audubon: wildlife walks and nature programs around Boston-area
  sanctuaries.
- Boston Harbor Now and Boston Harbor Islands: waterfront and island events.
- ArtsBoston: performing arts and cultural events throughout Greater Boston.

PDF-only seasonal schedules are lower priority because they require a more
fragile extraction pipeline. Prefer JSON, RSS, iCalendar, or stable semantic
HTML whenever available.
