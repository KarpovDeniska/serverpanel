/* ServerPanel — shared JS */

// HTMX global error handling
document.body.addEventListener('htmx:responseError', function(event) {
    const status = event.detail.xhr.status;
    if (status === 401) {
        window.location.href = '/login';
    }
});

// HTMX confirmation dialog
document.body.addEventListener('htmx:confirm', function(event) {
    if (event.detail.question) {
        event.preventDefault();
        if (confirm(event.detail.question)) {
            event.detail.issueRequest();
        }
    }
});
