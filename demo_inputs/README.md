# Synthetic demonstration inputs

These are invented records for demonstrating the interface, not official cadastral data.
The two surveys have the same concave boundary and competing recorded areas. The revenue
CSV has no geometry. Identifiers retain their leading zeros.

Create a project; upload survey_1.geojson and survey_2.geojson with EPSG:4326 (confirm CRS
if requested), then revenue.csv. Confirm parcel_id/recorded_area/ward/area_units mappings.
Select one record from each dataset in Source harmonization tools, compare them, and review
explicit boundary/default/per-field sources before accepting. Use Boundary editor to draw
and review a cut; merge its reviewed children; validate and publish the selected version.

Use documented synthetic capture dates such as 2024-01-01 and 2025-01-01 when testing dated
comparison. These dates describe the fixture scenario, not real survey acquisition dates.
