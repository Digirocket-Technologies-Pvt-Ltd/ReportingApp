"""Entry point for local development and production.

In dev mode (the default when running `python main.py`) the server hot-reloads
automatically when you save any .py / .html / .css / .js file in the project,
so you never have to Ctrl+C and restart by hand. The watcher uses `watchfiles`
(the same library FastAPI / uvicorn use for --reload) because Werkzeug's
built-in reloader crashes on Python 3.14 + Windows with WinError 10038.

Set the env var NO_RELOAD=1 to disable the watcher (e.g. when running under
gunicorn / waitress in production).
"""
import os
import sys

DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 5000


def _serve():
    """Actually run the WSGI server. Called by the watcher in dev mode (in
    a child process that gets killed + relaunched on every file change),
    or directly when NO_RELOAD is set."""
    # Allow http://localhost OAuth redirects in dev. Production should set
    # this to 0 (or unset) and use https.
    os.environ.setdefault('OAUTHLIB_INSECURE_TRANSPORT', '1')

    # Import inside the function so the *child* process imports a fresh copy
    # of the app on every restart - this is what picks up your edits.
    from app import app

    host = os.getenv('HOST', DEFAULT_HOST)
    port = int(os.getenv('PORT', str(DEFAULT_PORT)))

    try:
        from waitress import serve
        # 4 worker threads by default. Higher caps were stacking concurrent
        # PDF generations and OOM-killing the Render free-tier worker (~512
        # MB total). Bump via WAITRESS_THREADS on bigger plans.
        try:
            threads = max(1, int(os.getenv('WAITRESS_THREADS', '4')))
        except ValueError:
            threads = 4
        print(f' * Running on http://{host}:{port}  (waitress, {threads} threads)')
        serve(app, host=host, port=port, threads=threads)
    except ImportError:
        print(' * waitress not installed - using Flask dev server')
        print('   For best speed:  pip install waitress')
        # use_reloader=False because watchfiles is doing the reload
        # (Werkzeug's reloader is broken on Python 3.14 / Windows anyway).
        app.run(host=host, port=port, debug=True,
                use_reloader=False, threaded=True)


def _should_watch(change, path: str) -> bool:
    """File filter for watchfiles. Returns True if `path` should trigger a
    server restart. We only care about source files; skip caches, venvs,
    generated PDFs/screenshots, and anything inside .git."""
    p = path.replace('\\', '/').lower()
    # Skip noise that gets rewritten constantly or is irrelevant to the server
    skip_dirs = (
        '/.venv/', '/venv/', '/__pycache__/', '/.git/',
        '/node_modules/', '/.idea/', '/.vscode/',
        # Generated artefacts: the server writes these and we don't want a
        # restart loop every time a report is built.
        '/static/images/aivideo/', '/static/images/explanations/',
        '/static/images/session_',
    )
    if any(d in p for d in skip_dirs):
        return False
    if p.endswith('analytics_report.pdf') or p.endswith('analytics_report.pptx'):
        return False
    return p.endswith(('.py', '.html', '.css', '.js', '.json', '.env'))


if __name__ == '__main__':
    # Production / CI path: run once, no watcher.
    if os.getenv('NO_RELOAD'):
        _serve()
        sys.exit(0)

    # Dev path: hot-reload via watchfiles. If watchfiles isn't installed
    # we degrade to one-shot run with a friendly message.
    try:
        from watchfiles import run_process
    except ImportError:
        print(' * watchfiles not installed - running WITHOUT auto-reload')
        print('   To enable hot-reload:  pip install watchfiles')
        _serve()
        sys.exit(0)

    project_dir = os.path.dirname(os.path.abspath(__file__))
    print(' * Hot-reload enabled (watchfiles)')
    print('   Edit any .py / .html / .css / .js and the server restarts.')
    print('   Press Ctrl+C once to stop.')
    run_process(project_dir, target=_serve, watch_filter=_should_watch)
