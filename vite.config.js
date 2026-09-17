import { defineConfig } from "vite";
import path from "path";
import { viteStaticCopy } from "vite-plugin-static-copy";

export default defineConfig({
  css: {
    preprocessorOptions: {
      scss: {
        silenceDeprecations: [
          "import",
          "color-functions",
          "global-builtin",
          "if-function",
        ],
      },
    },
  },
  base: "/static/",
  plugins: [
    viteStaticCopy({
      targets: [
        {
          src: "assets/admin",
          dest: ".",
        },
        {
          src: "assets/images",
          dest: ".",
        },
      ],
    }),
  ],
  build: {
    outDir: path.resolve(__dirname, "./static"),
    emptyOutDir: true,
    manifest: "manifest.json",
    rollupOptions: {
      input: {
        index: path.resolve(__dirname, "./assets/index.js"),
        home: path.resolve(__dirname, "./assets/js/pages/home.js"),
        home_no_region: path.resolve(
          __dirname,
          "./assets/js/pages/home_no_region.ts",
        ),
        region_index: path.resolve(
          __dirname,
          "./assets/js/pages/region_index.ts",
        ),
        region_edit: path.resolve(
          __dirname,
          "./assets/js/pages/region_edit.ts",
        ),
        image_detail: path.resolve(
          __dirname,
          "./assets/js/pages/image_detail.js",
        ),
        map_display: path.resolve(
          __dirname,
          "./assets/js/components/map_display/index.ts",
        ),
        album_detail: path.resolve(
          __dirname,
          "./assets/js/pages/album_detail.js",
        ),
        collection_detail: path.resolve(
          __dirname,
          "./assets/js/pages/collection_detail.js",
        ),
        search: path.resolve(__dirname, "./assets/js/pages/search.js"),
        browse_subjects: path.resolve(
          __dirname,
          "./assets/js/pages/browse_subjects.ts",
        ),
        subjects_map: path.resolve(
          __dirname,
          "./assets/js/pages/subjects_map/index.ts",
        ),
        from_above: path.resolve(__dirname, "./assets/js/pages/from_above.js"),
        georeference_interface: path.resolve(
          __dirname,
          "./assets/js/pages/georeference_interface/index.ts",
        ),
        from_above_georeference_interface: path.resolve(
          __dirname,
          "./assets/js/pages/from_above_georeference_interface.js",
        ),
        stats: path.resolve(__dirname, "./assets/js/pages/stats.js"),

        subject_detail: path.resolve(
          __dirname,
          "./assets/js/pages/subject_detail.js",
        ),
        similar_images: path.resolve(
          __dirname,
          "./assets/js/pages/similar_images.js",
        ),
        user_georeferences: path.resolve(
          __dirname,
          "./assets/js/pages/user_georeferences.js",
        ),
        map_detail: path.resolve(__dirname, "./assets/js/pages/map_detail.ts"),
        layer_edit: path.resolve(__dirname, "./assets/js/pages/layer_edit.ts"),
        activity_feed: path.resolve(
          __dirname,
          "./assets/js/pages/activity_feed.js",
        ),
        directory_create: path.resolve(
          __dirname,
          "./assets/js/pages/directory_create.js",
        ),
        directory_edit: path.resolve(
          __dirname,
          "./assets/js/pages/directory_edit.js",
        ),
        directory_view: path.resolve(
          __dirname,
          "./assets/js/pages/directory_view.js",
        ),
        page_ocr: path.resolve(__dirname, "./assets/js/pages/page_ocr.js"),
        featured_image_edit: path.resolve(
          __dirname,
          "./assets/js/pages/featured_image_edit.js",
        ),
        entry_validate: path.resolve(
          __dirname,
          "./assets/js/pages/entry_validate.js",
        ),
      },
      output: {
        entryFileNames: `js/[name]-[hash].js`,
        chunkFileNames: `js/[name]-[hash].js`,
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith(".css")) {
            return "css/[name]-[hash].css";
          }
          return "assets/[name]-[hash][extname]";
        },
      },
    },
  },
});
