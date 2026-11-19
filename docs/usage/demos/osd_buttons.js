// Standalone version of assets/js/components/osd_buttons.ts. The demo runs as
// a native ES module with no bundler, so it cannot import the TypeScript
// component directly. Keep this file in sync with that one.

const zoomButtons = [
  { icon: "fa-plus", title: "Zoom in", action: (v) => v.viewport.zoomBy(1.5) },
  {
    icon: "fa-minus",
    title: "Zoom out",
    action: (v) => v.viewport.zoomBy(0.667),
  },
  { icon: "fa-home", title: "Reset view", action: (v) => v.viewport.goHome() },
  {
    icon: "fa-expand",
    title: "Full screen",
    action: (v) => v.setFullScreen(!v.isFullPage()),
  },
];

function makeButton(icon, title, onClick) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "osd-btn";
  btn.title = title;
  const i = document.createElement("i");
  i.className = `fas ${icon}`;
  btn.appendChild(i);
  btn.addEventListener("click", onClick);
  return btn;
}

function setViewerInteractive(viewer, enabled) {
  viewer.setMouseNavEnabled(enabled);
  // OSD sets touch-action: none on both canvas and container; restore it when locked
  // so swipe/scroll gestures fall through to the page.
  const touchAction = enabled ? "none" : "auto";
  viewer.canvas.style.touchAction = touchAction;
  viewer.container.style.touchAction = touchAction;
}

export function addViewerButtons(viewer, container) {
  container.style.position = "relative";

  // Start locked
  setViewerInteractive(viewer, false);

  // Locked state: a single unlock button. Unlocked state: zoom controls plus a
  // lock button. Exactly one group is visible at a time.
  const lockedGroup = document.createElement("div");
  lockedGroup.className = "osd-btn-group";

  const unlockedGroup = document.createElement("div");
  unlockedGroup.className = "osd-btn-group";
  unlockedGroup.style.display = "none";

  const setLocked = (locked) => {
    setViewerInteractive(viewer, !locked);
    lockedGroup.style.display = locked ? "flex" : "none";
    unlockedGroup.style.display = locked ? "none" : "flex";
  };

  lockedGroup.appendChild(
    makeButton("fa-search-plus", "Enable zoom", () => setLocked(false)),
  );

  for (const { icon, title, action } of zoomButtons) {
    unlockedGroup.appendChild(makeButton(icon, title, () => action(viewer)));
  }
  unlockedGroup.appendChild(
    makeButton("fa-lock", "Lock view", () => setLocked(true)),
  );

  container.appendChild(lockedGroup);
  container.appendChild(unlockedGroup);
}
