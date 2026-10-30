document.addEventListener("click", (event) => {
    const field = event.target;
    if (!(field instanceof HTMLInputElement) || field.type !== "datetime-local" ||
        field.disabled || field.readOnly || typeof field.showPicker !== "function") {
        return;
    }

    try {
        field.showPicker();
    } catch {
        // Preserve the native control when the browser cannot open its picker here.
    }
});
