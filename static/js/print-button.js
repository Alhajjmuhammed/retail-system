// The Print button on a receipt, a purchase order or a statement. These are
// plain documents with no Alpine on them, and `onclick="window.print()"` is
// inline script: the policy the site sends blocks it, and the button did
// nothing on the live site.
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-print]").forEach((button) => {
    button.addEventListener("click", () => window.print());
  });
});
