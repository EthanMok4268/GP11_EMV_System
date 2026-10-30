(() => {
    const dialog = document.getElementById("election-deletion-dialog");
    const openButton = document.getElementById("open-election-deletion");
    const form = document.getElementById("election-deletion-form");
    const confirmButton = document.getElementById("confirm-election-deletion");
    if (!dialog || !openButton || !form || !confirmButton || typeof dialog.showModal !== "function") {
        return;
    }

    openButton.addEventListener("click", () => dialog.showModal());
    dialog.querySelectorAll("[data-cancel-deletion]").forEach((button) => {
        button.addEventListener("click", () => dialog.close());
    });
    form.addEventListener("submit", (event) => {
        // Only the explicit confirmation button may submit this destructive form.
        if (!dialog.open || event.submitter !== confirmButton) {
            event.preventDefault();
        }
    });
    confirmButton.disabled = false;
    openButton.disabled = false;
})();
