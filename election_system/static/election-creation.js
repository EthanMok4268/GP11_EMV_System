(() => {
    const form = document.getElementById("election-creation-form");
    if (!form) return;

    const rows = document.getElementById("candidate-rows");
    const template = document.getElementById("candidate-row-template");
    const addButton = document.getElementById("add-candidate");
    const createButton = document.getElementById("create-election");
    const dialog = document.getElementById("empty-roster-dialog");
    const fallback = document.getElementById("empty-roster-confirmation");
    const supportsDialog = typeof dialog.showModal === "function";
    let nextIndex = rows.children.length;

    function validateElection() {
        const title = form.elements.title;
        const start = form.elements.start_time;
        const end = form.elements.end_time;
        title.setCustomValidity(title.value.trim() ? "" : "Election title is required.");
        end.setCustomValidity(start.value && end.value && start.value >= end.value ?
            "The end time must be after the start time." : "");
    }
    for (const field of [form.elements.title, form.elements.start_time, form.elements.end_time]) {
        field.addEventListener("input", validateElection);
    }

    const candidateRows = () => [...rows.querySelectorAll("[data-candidate-row]")];
    const values = (row) => ({
        name: row.querySelector("[data-candidate-name]").value.trim(),
        manifesto: row.querySelector("[data-candidate-manifesto]").value.trim(),
    });

    function updateRows() {
        const entries = candidateRows();
        entries.forEach((row, index) => {
            row.querySelector("[data-candidate-number]").textContent = index + 1;
            const remove = row.querySelector("[data-remove-candidate]");
            remove.disabled = false;
            remove.setAttribute("aria-label", `Remove candidate ${index + 1}`);
        });
        const count = entries.filter((row) => {
            const candidate = values(row);
            return candidate.name && candidate.manifesto;
        }).length;
        document.getElementById("candidate-count").textContent = `${count} complete`;
        document.getElementById("no-candidate-entries").hidden = entries.length !== 0;
    }

    function validateRow(row) {
        const candidate = values(row);
        const name = row.querySelector("[data-candidate-name]");
        const manifesto = row.querySelector("[data-candidate-manifesto]");
        const message = row.querySelector("[data-candidate-error]");
        const partial = Boolean(candidate.name || candidate.manifesto) &&
            (!candidate.name || !candidate.manifesto);
        const numeric = /^\p{Number}+$/u.test(candidate.name);
        const error = partial ? "Candidate name and manifesto are both required." :
            numeric ? "Candidate name must contain text and cannot be only a number." : "";
        name.setCustomValidity((partial && !candidate.name) || numeric ? error : "");
        manifesto.setCustomValidity(partial && !candidate.manifesto ? error : "");
        for (const field of [name, manifesto]) {
            field.setAttribute("aria-invalid", String(!field.validity.valid));
        }
        message.textContent = error;
        message.hidden = !error;
        return !error;
    }

    addButton.addEventListener("click", () => {
        const fragment = template.content.cloneNode(true);
        fragment.querySelectorAll("[id], [for], [aria-describedby]").forEach((element) => {
            for (const attribute of ["id", "for", "aria-describedby"]) {
                if (element.hasAttribute(attribute)) {
                    element.setAttribute(attribute, element.getAttribute(attribute).replace("__INDEX__", nextIndex));
                }
            }
        });
        nextIndex += 1;
        rows.appendChild(fragment);
        updateRows();
        rows.lastElementChild.querySelector("[data-candidate-name]").focus();
    });

    rows.addEventListener("click", (event) => {
        const remove = event.target.closest("[data-remove-candidate]");
        if (!remove) return;
        const row = remove.closest("[data-candidate-row]");
        const focusRow = row.nextElementSibling || row.previousElementSibling;
        row.remove();
        updateRows();
        (focusRow ? focusRow.querySelector("[data-candidate-name]") : addButton).focus();
    });

    rows.addEventListener("input", (event) => {
        const row = event.target.closest("[data-candidate-row]");
        if (!row) return;
        validateRow(row);
        updateRows();
    });

    function dismissConfirmation() {
        if (dialog.open) dialog.close();
        fallback.hidden = true;
        createButton.focus();
    }

    dialog.querySelectorAll("[data-cancel-creation]").forEach((button) => {
        button.addEventListener("click", dismissConfirmation);
    });
    dialog.addEventListener("close", () => createButton.focus());
    dialog.addEventListener("keydown", (event) => {
        if (event.key !== "Tab") return;
        const buttons = [...dialog.querySelectorAll("button:not([disabled])")];
        const first = buttons[0];
        const last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
    // The native dialog also dismisses on Esc; close restores the form's focus.

    form.addEventListener("submit", (event) => {
        if (event.submitter?.name === "cancel_without_candidates") {
            event.preventDefault();
            dismissConfirmation();
            return;
        }
        validateElection();
        const valid = candidateRows().map(validateRow).every(Boolean);
        if (!valid || !form.reportValidity()) {
            event.preventDefault();
            if (dialog.open) dialog.close();
            form.reportValidity();
            return;
        }
        const hasCandidates = candidateRows().some((row) => {
            const candidate = values(row);
            return candidate.name && candidate.manifesto;
        });
        if (!hasCandidates && event.submitter?.name !== "confirm_without_candidates") {
            event.preventDefault();
            if (supportsDialog) {
                dialog.showModal();
            } else {
                fallback.hidden = false;
                fallback.querySelector("button").focus();
            }
        }
    });

    updateRows();
    addButton.disabled = false;
    if (!fallback.hidden && supportsDialog) {
        fallback.hidden = true;
        dialog.showModal();
    }
})();
