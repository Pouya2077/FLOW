// The gear on pages without a map (the About page): it opens and closes the settings, and Escape
// closes them. map.js has its own copy, since Escape there also closes popups and pathfinding.
const settingsToggle = document.querySelector(".settings-toggle");
const settingsMenu = document.getElementById("settings-menu");

function setSettingsOpen(open) {
  settingsToggle.setAttribute("aria-expanded", String(open));
  settingsMenu.hidden = !open;
}

settingsToggle?.addEventListener("click", () => {
  setSettingsOpen(settingsToggle.getAttribute("aria-expanded") !== "true");
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && settingsMenu && !settingsMenu.hidden) {
    setSettingsOpen(false);
    settingsToggle.focus();
  }
});
