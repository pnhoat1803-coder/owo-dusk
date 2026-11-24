/* Login page: show or hide the password, with its labels in the current language. A separate file because the page's
   Content-Security-Policy (website/api_v2.py, CSP) allows scripts from this origin only, so an inline script would not run. */
(function () {
  "use strict";
  var pw = document.getElementById("pw");
  var btn = document.getElementById("toggle-pw");
  if (!pw || !btn) return;
  function t(key) { return window.DuskI18n ? window.DuskI18n.t(key) : key; }
  btn.addEventListener("click", function () {
    var reveal = pw.type === "password";
    pw.type = reveal ? "text" : "password";
    var label = t(reveal ? "login.hide" : "login.show");
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
    btn.setAttribute("aria-pressed", reveal ? "true" : "false");
    btn.classList.toggle("active", reveal);
  });
})();
