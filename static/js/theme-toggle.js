/* DigiRocket dark mode. Applied on every page to match the brand
   (black + lime). The old floating sun/moon toggle button has been
   removed for a cleaner, more professional UI. */
(function () {
    function init() {
        document.body.classList.add('dark-mode');
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
