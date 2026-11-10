# Site Settings

An instance's site settings can be accessed at `/admin/images/sitesettings/1/change/`.

## Homepage Content

The title and subtitle as presented in the hero on the site's landing page.

## Footer

An HTML blob to be inserted into the footer of the site.
This should include links to Yesterdays source code, admin contact information, etc.

## Contact

The administrator should consider putting their email into this field.
An email is required for Yesterdays to successfully geocode user requests using the Nominatim geocoder.

## Maps and geocoding

There is no sitewide default map view or search area.
When a visitor has selected a region in the navbar, maps open on that region and the geocoder searches within it; with no region selected, maps show the whole world and the geocoder searches worldwide.

Each region's starting view and search area are configured on the region itself, at `/admin/regions/region/`:

- **Map bounds** is the initial map viewport. Left blank, maps center on the region's coordinate location at a fixed zoom.
- **Geocoder bounds** limits Nominatim searches, so that when users search things like `123 Main Street` they're more likely to find the address they're looking for in their area. Left blank, the map bounds are used instead; if both are blank, searches are unbounded.
  The `detect_addresses` command skips images that have no region, or whose region has neither, rather than geocoding street addresses worldwide.
