# Searching

The search page features four search modes: semantic, text, reverse image, and in view.

## Semantic Search

Yesterdays provides a "semantic search" feature on [the search page](https://yesterdays.today/search/), which allows the user to search based on the **content** of the images rather than any textual metadata. This uses a [CLIP](https://en.wikipedia.org/wiki/Contrastive_Language-Image_Pre-training) model, allowing users to search by describing what the images are of.

Try out searches like:

- [firefighters responding to the scene](https://yesterdays.today/search/?q=firefighters+responding+to+the+scene&mode=semantic)
- [stained-glass windows](https://yesterdays.today/search/?q=stained-glass+windows&mode=semantic)
- [truck on a highway](https://yesterdays.today/search/?q=truck+on+a+highway&mode=semantic)
- etc.

### Find Similar Images

On an image page or subject page, there is a button to view "similar" images to that image or subject.
This list uses the same CLIP model described above, providing images that look similar to the given image or set of images.
For example, a photo of an overturned car might yield [other photos of overturned cars](https://yesterdays.today/10470/similar/).

## Text Search

On [the search page](https://yesterdays.today/search/), there is also a classic text search, which indexes:

- Image titles
- Image descriptions
- Comments and georeference notes


This mode performs a distance search across the entire Yesterdays database.
For example, [a search for "118 East Cary"](https://yesterdays.today/search/?q=118+East+Cary&mode=text) will surface an image with the title "117 East Cary Street"

## Reverse Image Search

!!! info
    Reverse image search is currently only available to logged-in users.
    It is free and easy to make an account with OpenStreetMap!

The reverse image search feature allows you to upload your own image, and find similar images in Yesterdays. Currently, only JPEG, PNG, GIF, and WEBP image formats are supported.

You can upload your image by doing any of the following:

- clicking on the "Select File" button to select an image file from your filesystem,
- dragging an image file into the upload area from e.g. your file browser, or
- pasting an image from your clipboard using CTRL+V or CMD+V

Images are searched using the same CLIP model described in the Semantic Search section above.

## In View Search

In View search lets you click on a map and quickly find images of that location.
Images are returned in order of distance from the selected point.

When someone georeferences a photograph with the direction the camera was pointed, that image is only shown if your selected location falls within 40° of where it was pointing.
If a georeference does not record a direction, it will still be included in In View search results.

For now, "From Above" images are not included in In View search results.
