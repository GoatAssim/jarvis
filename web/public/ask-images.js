/* Pictures in the Ask Jarvis composer: the pure helpers (list limits, picture URLs, what a saved
 * exchange shows). The DOM wiring lives in app.js; everything here runs in a bare `window` (vm), so
 * tests/verify_ask_images.js can call it without a browser.
 *
 * The limits mirror web/ask-images.js (the server's own check) and jarvis/channels/media_in.py; the
 * server and the CLI re-check everything -- this file only keeps a doomed message from being sent.
 */
(function () {
  "use strict";

  var MAX_IMAGES = 3;
  var MAX_BYTES = 8 * 1024 * 1024;
  // The words a message that is only pictures is stored with (server.js ASK_IMAGE_PLACEHOLDER).
  var PLACEHOLDER = "(sent a picture)";
  var ACCEPT = "image/png,image/jpeg,image/gif,image/webp";
  var TYPES = { "image/png": 1, "image/jpeg": 1, "image/gif": 1, "image/webp": 1 };
  var EXT = /\.(png|jpe?g|gif|webp)$/i;
  var ID_RE = /^ci_[0-9a-f]{24}\.(png|jpg|gif|webp)$/;

  function isPicture(file) {
    if (!file) return false;
    var type = String(file.type || "").toLowerCase();
    if (type) return !!TYPES[type];
    return EXT.test(String(file.name || ""));   // some pasted / dragged files have no type
  }

  function label(file) {
    return String((file && file.name) || "picture").split(/[\\/]/).pop().slice(0, 80) || "picture";
  }

  // list: the files already attached; incoming: files just chosen / pasted / dropped.
  // Returns { list, rejected: [{ name, reason }] } and never mutates its arguments.
  function addFiles(list, incoming) {
    var out = (list || []).slice();
    var rejected = [];
    var files = Array.prototype.slice.call(incoming || []);
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      var name = label(f);
      if (!isPicture(f)) {
        rejected.push({ name: name, reason: "isn't a PNG, JPEG, GIF or WebP picture" });
      } else if (Number(f.size) > MAX_BYTES) {
        rejected.push({ name: name, reason: "is over the " + (MAX_BYTES / 1048576) + " MB limit" });
      } else if (out.length >= MAX_IMAGES) {
        rejected.push({ name: name, reason: "would be more than " + MAX_IMAGES + " pictures in one message" });
      } else {
        out.push(f);
      }
    }
    return { list: out, rejected: rejected };
  }

  // "name is over..., name2 isn't ..." for one toast; "" when nothing was rejected.
  function rejectedMessage(rejected) {
    if (!rejected || !rejected.length) return "";
    return rejected.map(function (r) { return r.name + " " + r.reason; }).join("; ") + ".";
  }

  // "data:image/png;base64,AAAA" -> "AAAA"
  function base64Of(dataUrl) {
    var s = String(dataUrl || "");
    var at = s.indexOf(",");
    return at < 0 ? "" : s.slice(at + 1);
  }

  function pictureUrl(id) {
    return ID_RE.test(String(id || "")) ? "/api/conv-images/" + id : "";
  }

  // A saved exchange's picture references -> [{ src, name }], valid ids only.
  function picturesOf(exchange) {
    var refs = exchange && Array.isArray(exchange.images) ? exchange.images : [];
    var out = [];
    for (var i = 0; i < refs.length && out.length < MAX_IMAGES; i++) {
      var r = refs[i];
      var src = r && pictureUrl(r.id);
      if (src) out.push({ src: src, name: label({ name: r.name }) });
    }
    return out;
  }

  // The words to draw in the bubble for a saved exchange. `user` also holds the picture
  // descriptions (that is what the model was asked), so when pictures came with the message the
  // stored `shown` -- what was typed -- is drawn instead; a picture-only message draws no words,
  // the same as it did the moment it was sent.
  function textOf(exchange) {
    if (!exchange) return "";
    if (picturesOf(exchange).length) {
      var shown = typeof exchange.shown === "string" ? exchange.shown : "";
      return shown.trim() === PLACEHOLDER ? "" : shown;
    }
    return exchange.user || "";
  }

  window.JarvisAskImages = {
    MAX_IMAGES: MAX_IMAGES,
    MAX_BYTES: MAX_BYTES,
    PLACEHOLDER: PLACEHOLDER,
    ACCEPT: ACCEPT,
    isPicture: isPicture,
    addFiles: addFiles,
    rejectedMessage: rejectedMessage,
    base64Of: base64Of,
    pictureUrl: pictureUrl,
    picturesOf: picturesOf,
    textOf: textOf,
  };
})();
