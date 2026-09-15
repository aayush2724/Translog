"use strict";

/* Sign-in behaviour. The CSP forbids inline script, so this lives in its own
   file served from the same origin. It POSTs the credentials as JSON (the
   server's cross-site guard requires application/json), lets the server set the
   HttpOnly session cookie, and on success navigates to the dashboard. It never
   stores the password and never reads the cookie — that is the server's. */

(function () {
  const form = document.getElementById("login-form");
  const errorEl = document.getElementById("login-error");
  const button = document.getElementById("login-submit");

  function showError(message) {
    errorEl.textContent = message;
    errorEl.hidden = false;
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    errorEl.hidden = true;
    button.disabled = true;

    const username = document.getElementById("login-username").value.trim();
    const password = document.getElementById("login-password").value;

    try {
      const response = await fetch("/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (response.ok) {
        /* The server has set the session cookie; go to the desk. */
        window.location.assign("/");
        return;
      }
      const payload = await response.json().catch(() => ({}));
      showError(
        payload.error === "invalid credentials"
          ? "That password was not recognised. Check it and try again."
          : payload.error || "Sign-in failed. Please try again.",
      );
    } catch (err) {
      showError("The server did not respond. Please try again.");
    }
    button.disabled = false;
  });
})();
