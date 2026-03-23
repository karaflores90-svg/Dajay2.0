document.addEventListener("DOMContentLoaded", () => {
    window.setTimeout(() => {
        document.documentElement.classList.remove("app-loading");
        document.documentElement.classList.add("app-ready");
    }, 500);

    const modal = document.getElementById("trailerModal");
    if (modal) {
        modal.addEventListener("click", (event) => {
            if (event.target === modal) {
                window.closeTrailer();
            }
        });
    }

    document.querySelectorAll(".flash-message").forEach((message, index) => {
        window.setTimeout(() => {
            message.classList.add("is-hiding");
            window.setTimeout(() => {
                message.remove();
            }, 220);
        }, 3600 + index * 250);
    });

    document.querySelectorAll("form").forEach((form) => {
        form.addEventListener("submit", (event) => {
            const submitter = event.submitter || form.querySelector('button[type="submit"], input[type="submit"]');
            if (!submitter || submitter.dataset.loadingApplied === "true") {
                if (submitter) {
                    event.preventDefault();
                }
                return;
            }

            submitter.dataset.loadingApplied = "true";
            submitter.classList.add("is-loading");
            const loadingText = submitter.dataset.loadingText || "Please wait...";

            if (submitter.tagName === "INPUT") {
                submitter.value = loadingText;
            } else {
                submitter.dataset.originalText = submitter.innerHTML;
                submitter.innerHTML = loadingText;
            }

            form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach((button) => {
                button.disabled = true;
            });
            submitter.disabled = true;
        });
    });
});

document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
        window.closeTrailer();
    }
});

window.closeTrailer = function closeTrailer() {
    const modal = document.getElementById("trailerModal");
    const video = document.getElementById("trailerVideo");

    if (video) {
        video.src = "";
    }

    if (modal) {
        modal.style.display = "none";
    }
};
