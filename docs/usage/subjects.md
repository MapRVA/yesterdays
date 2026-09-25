# Subjects

Subjects represent the entities in images. They could be:

- [buildings](https://yesterdays.today/subjects/?category=Q41176),
- [people](https://yesterdays.today/subjects/?category=Q215627),
- [natural phenomena](https://yesterdays.today/subjects/?category=Q1322005),
- [car models](https://yesterdays.today/subjects/?category=Q3231690),
- [parks](https://yesterdays.today/subjects/?category=Q22698),
- etc.

In general, a subject needs to be a physical, observable, _thing_.



## Exploring Subjects

The [subjects page](https://yesterdays.today/subjects/) provides two ways to explore subjects:

- A map showing buildings, parks, etc. using [OpenStreetMap](https://openstreetmap.org) data.
- An autocomplete-enabled search bar where you can filter by category.

As demonstrated by the links at the top of this page, categories are determined by Wikidata relationships such as [`P31 instance of`](https://www.wikidata.org/wiki/Property:P31) or [`P279 subclass of`](https://www.wikidata.org/wiki/Property:P279). This system of categories allows us to tag specific things like [Morgan Fountain](https://yesterdays.today/subjects/morgan-fountain/), and have those photos also show up on the page for [fountain](https://yesterdays.today/subjects/fountain/). This works because [`Q137179642 Morgan Fountain`](https://www.wikidata.org/wiki/Q137179642) is an instance of [`Q483453 fountain`](https://www.wikidata.org/wiki/Q483453).

## Tagging Subjects

!!! info
    Tagging subjects is only available to logged-in users.
    It is free and easy to make an account with OpenStreetMap!

Subjects are tagged using Wikidata IDs.
In order to add a subject to Yesterdays, you must first find or create a Wikidata item for that subject.
In doing so you must respect the [inclusion criteria for Wikidata](https://www.wikidata.org/wiki/Wikidata:Notability "a clearly identifiable conceptual or material entity that can be described using serious and publicly available references").

Subjects may be added from the image detail page, or the georeference interface. Click on the "Add Subject" button, and then type in the Wikidata ID for the subject you want to add.
If that subject has been added to an image before, you may also start typing its name, and it will appear as an autocomplete result in a dropdown.

You may also bulk-add subjects to images in any image grid on Yesterdays by selecting the images and choosing the "Add Subject" bulk action from the menu.
