(() => {
    const entries = document.getElementById("audit-log-entries");
    const loadMore = document.getElementById("load-more-audit-logs");
    const status = document.getElementById("audit-log-status");
    if (!entries || !loadMore || !status) return;

    let loading = false;
    loadMore.addEventListener("click", async () => {
        if (loading || !loadMore.dataset.nextUrl) return;
        loading = true;
        loadMore.disabled = true;
        entries.setAttribute("aria-busy", "true");
        status.classList.remove("text-danger");
        status.textContent = "Loading more audit entries...";

        try {
            const response = await fetch(loadMore.dataset.nextUrl, {
                headers: {Accept: "application/json"},
            });
            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }
            if (!response.ok) throw new Error("Unable to load audit entries");
            const batch = await response.json();
            const fragment = document.createDocumentFragment();
            for (const log of batch.entries) {
                const item = document.createElement("li");
                item.className = "list-group-item";
                item.setAttribute("data-audit-entry", "");

                const content = document.createElement("div");
                content.className = "text-break";
                const username = document.createElement("strong");
                username.textContent = log.username;
                const action = document.createElement("span");
                action.textContent = log.action;
                content.append(username, ": ", action);

                const timestamp = document.createElement("div");
                timestamp.className = "text-muted small";
                timestamp.textContent = log.timestamp;
                item.append(content, timestamp);
                fragment.appendChild(item);
            }
            entries.appendChild(fragment);
            loadMore.dataset.nextUrl = batch.next_url || "";
            loadMore.hidden = !batch.next_url;
            status.textContent = batch.next_url ?
                `Loaded ${batch.entries.length} more audit entries.` : "All audit entries are displayed.";
        } catch {
            status.classList.add("text-danger");
            status.textContent = "Unable to load more audit entries. Please try again.";
        } finally {
            loading = false;
            loadMore.disabled = false;
            entries.setAttribute("aria-busy", "false");
        }
    });
})();
