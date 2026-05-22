/* DigiRocket dark/light mode toggle.
   Adds a floating 🌙/☀️ button, remembers the choice in localStorage. */
(function () {
    function currentTheme() {
        // Default to dark to match digirocket.io (black + lime)
        return localStorage.getItem('theme') || 'dark';
    }

    function applyTheme(theme) {
        if (theme === 'dark') {
            document.body.classList.add('dark-mode');
        } else {
            document.body.classList.remove('dark-mode');
        }
        var btn = document.getElementById('themeToggleBtn');
        if (btn) btn.innerHTML = (theme === 'dark') ? '☀️' : '🌙';
    }

    function init() {
        applyTheme(currentTheme());

        var btn = document.createElement('button');
        btn.id = 'themeToggleBtn';
        btn.type = 'button';
        btn.title = 'Toggle dark / light mode';
        btn.innerHTML = (currentTheme() === 'dark') ? '☀️' : '🌙';
        btn.style.cssText = [
            'position:fixed', 'bottom:88px', 'right:24px', 'z-index:99999',
            'width:52px', 'height:52px', 'border-radius:50%', 'border:none',
            'cursor:pointer', 'font-size:22px', 'background:#00AEAF', 'color:#fff',
            'box-shadow:0 6px 20px rgba(0,0,0,0.3)', 'display:flex',
            'align-items:center', 'justify-content:center', 'transition:transform .2s'
        ].join(';');
        btn.addEventListener('mouseenter', function () { btn.style.transform = 'scale(1.1)'; });
        btn.addEventListener('mouseleave', function () { btn.style.transform = 'scale(1)'; });
        btn.addEventListener('click', function () {
            var next = (currentTheme() === 'dark') ? 'light' : 'dark';
            localStorage.setItem('theme', next);
            applyTheme(next);
        });
        document.body.appendChild(btn);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
