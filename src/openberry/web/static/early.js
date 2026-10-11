/* Runs before first paint so CSS can tell JS-enhanced pages apart (wizard steps, mobile nav). */
document.documentElement.classList.replace("no-js", "js");

/* A tab or a list action is reloading this page and app.js (initKeepScroll) will put it back where the user was:
   hide the content until then, so the top of the page doesn't flash first. One second at most. The rule matches
   keepScrollApplies in app.js. */
try {
  var keep = JSON.parse(sessionStorage.getItem("openberry-keep-scroll") || "null");
  var age = keep ? Date.now() - keep.at : -1;
  if (keep && keep.to === location.pathname + location.search && !location.hash && age >= 0 && age < 15000) {
    document.documentElement.classList.add("keep-scroll");
    setTimeout(function () { document.documentElement.classList.remove("keep-scroll"); }, 1000);
  }
} catch (err) { /* storage blocked or a bad value: start at the top as usual */ }
