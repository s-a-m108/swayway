# swayway
# SwayWay

Get to the polls your way. SwayWay helps Bay Area voters find an official vote center near their address, pick how they'll get there (from walking to a dune buggy), and get real directions for Election Day, Tuesday, Nov 3, 2026.

## Run it locally

Requires Python 3 and `curl` (both come with macOS).

```bash
python3 server.py
```

Then open http://localhost:8642.

## How it works

| Step | What happens | Data source |
|---|---|---|
| Address | City/ZIP fill from a built-in Bay Area list; live street suggestions with ZIP | `us-data.js`, [Photon](https://photon.komoot.io) (OpenStreetMap) |
| Polling location | Official vote centers in the voter's county, nearest first, with hours, drop-off and same-day registration | California Secretary of State vote center lookup; U.S. Census geocoder for county |
| Filters | Accessibility, getting there, services, crowds, comfort, special requests, neighborhoods to avoid, building type | OpenStreetMap (via Overpass), `hoods.js` |
| Transportation | 295 options, pick one or more | `vehicles.js` |
| Directions | Per-method routing with neighborhoods avoided; real transit itineraries on Nov 3 schedules | [Valhalla](https://valhalla1.openstreetmap.de), [Transitous](https://transitous.org) |
| Rally your people | Shareable message built from the plan | — |

## Files

- `index.html` — the app (single page)
- `server.py` — local server and API proxy (`/api/vote-centers`, `/api/access`, `/api/route`)
- `us-data.js` — Bay Area cities and ZIP codes
- `hoods.js` — Bay Area neighborhoods with approximate centers and radii
- `vehicles.js` — transportation options, categories and routing mode
- `favicon.png`

## Limits

- Bay Area (nine counties) only.
- Accessibility, parking, transit and comfort details come from OpenStreetMap and can be incomplete.
- Neighborhood avoid zones are approximate circles, and transit routes can't avoid neighborhoods.
- Transit times come from published schedules; check for changes before Election Day.
- This information may not be accurate. Confirm your polling place and hours with your county elections office.

SwayWay is nonpartisan and free, but does intend to make money.
