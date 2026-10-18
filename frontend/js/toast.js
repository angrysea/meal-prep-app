// One toast element per page, created on first use, so a page doesn't need
// its own markup for it. Styled by .toast in style.css (menu-flyer.html,
// which doesn't load style.css, carries its own copy of those rules).
// Message text is set via textContent, so it's safe to include
// customer-entered values (names, emails) in it.

let hideTimeout;

export function showToast(message, { error = false } = {}) {
  let toast = document.getElementById("toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "toast";
    toast.className = "toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  toast.classList.toggle("toast-error", error);
  toast.classList.add("visible");
  clearTimeout(hideTimeout);
  // Errors stay up longer - they usually need reading, not just noticing.
  hideTimeout = setTimeout(() => toast.classList.remove("visible"), error ? 6000 : 3000);
}
