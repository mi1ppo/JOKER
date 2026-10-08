function saveSessionToken(token) {
    localStorage.setItem('sessionToken', token);
}

function getSessionToken() {
    return localStorage.getItem('sessionToken');
}

// Автоматически восстановить токен при загрузке страницы
window.addEventListener('load', function() {
    const token = getSessionToken();
    if (token) {
        const urlParams = new URLSearchParams(window.location.search);
        if (!urlParams.has('token')) {
            window.location.href = `/?token=${token}`;
        }
    }
});

// Сохранить токен при его получении
window.addEventListener('message', function(event) {
    if (event.data && event.data.type === 'sessionToken') {
        saveSessionToken(event.data.token);
    }
});