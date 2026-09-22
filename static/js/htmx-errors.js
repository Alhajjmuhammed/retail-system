// A server error or a lost connection inside a pop-up used to do nothing
// visible. Say so, in the pop-up, instead of leaving the button dead.
document.addEventListener("htmx:responseError", (event) => {
  const modal = document.getElementById("modal");
  if (!modal) return;
  const status = event.detail.xhr ? event.detail.xhr.status : 0;
  const why = status >= 500 ? "Something went wrong on our side."
            : status === 403 ? "You are not allowed to do that."
            : "The server refused it.";

  // Built as nodes, not as a string with an onclick in it: inline handlers
  // are script too, and the policy this site sends does not allow them.
  const backdrop = document.createElement("div");
  backdrop.setAttribute("x-data", "");
  backdrop.className = "fixed inset-0 z-50 flex items-end justify-center bg-ink-950/50 p-3 sm:items-start sm:p-6 sm:pt-16";

  const box = document.createElement("div");
  box.className = "w-full max-w-md rounded-2xl bg-white p-5 shadow-float";

  const heading = document.createElement("p");
  heading.className = "text-base font-semibold text-ink-900";
  heading.textContent = "That did not go through";

  const detail = document.createElement("p");
  detail.className = "mt-1 text-sm text-ink-600";
  detail.textContent = why + " Nothing was saved. Reload the page and try again.";

  const row = document.createElement("div");
  row.className = "mt-4 flex justify-end";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "btn btn-secondary";
  close.textContent = "Close";
  close.addEventListener("click", () => backdrop.remove());

  row.append(close);
  box.append(heading, detail, row);
  backdrop.append(box);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) backdrop.remove(); });
  document.addEventListener("keydown", function escape(e) {
    if (e.key === "Escape") { backdrop.remove(); document.removeEventListener("keydown", escape); }
  });

  modal.replaceChildren(backdrop);
});
