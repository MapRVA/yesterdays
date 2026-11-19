# Contributing Documentation

One of the most valuable ways you can contribute to Yesterdays is by reviewing and contributing to our documentation.

The source for this documentation site lives in [the Yesterdays git repository](https://github.com/MapRVA/yesterdays), in the `/docs/` folder.
It is additionally configured in `/zensical.toml`.

## Hosting locally

Some interactive demos run the main site's own frontend code, which is bundled into `/docs/usage/demos/` with [Bun](https://bun.sh).
Build them once (and again after changing the frontend code they use):

```
bun install
bun run docs:demos
```

Then use the following command to host this site locally:

```
uv run zensical serve
```

The site will be live at `http://localhost:8000`.
Changes in the `/docs/` directory should trigger automatic refreshes of the site as you view it in your browser.

Please note that the main Yesterdays site also uses port 8000 by default.
If you'd like to run both at the same time, pick a different port for Zensical:

```
uv run zensical serve -a localhost:8080
```

## Code blocks

We strive to provide all code examples in Python and R.
You may open an issue in our repository to request another language.

Code blocks are formatted using a variety of tricks [as documented by Zensical](https://zensical.org/docs/authoring/code-blocks/).
