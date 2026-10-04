# vendor/

Local fallbacks for CDN scripts, used only when the CDN copy fails to load.

| File | What | Pinned to |
|---|---|---|
| `highlight.min.js` | highlight.js "common languages" build, for code-block colouring | the `@highlightjs/cdn-assets@X.Y.Z` tag in `../index.html` |

`highlight.min.js` is **not committed by the patch that introduced this folder** —
fetch it once (needs network):

```
node web/scripts/vendor-highlightjs.mjs
```

then commit the file. Until it exists, an offline / CDN-blocked page simply shows
plain monospace code (Copy still works). To bump highlight.js, change the version
in `../index.html` and rerun the script — the two must move together.
