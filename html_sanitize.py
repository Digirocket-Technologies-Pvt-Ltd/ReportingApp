"""Tiny HTML sanitizer for user-entered rich text (ticket descriptions /
messages). Allows ONLY a small whitelist of formatting tags with NO
attributes — so there's no room for <script>, onerror=, href=javascript:,
etc. Everything else is escaped to plain text.

Used via the Jinja `safe_html` filter so even data stored before this
existed renders safely (plain text passes through untouched, with newlines
turned into <br>).
"""
from html.parser import HTMLParser
from html import escape
from markupsafe import Markup

# Block + inline tags a basic editor (Bold/Italic/Underline/List) emits.
_ALLOWED = {'b', 'strong', 'i', 'em', 'u', 'br', 'p', 'div', 'ul', 'ol', 'li'}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in _ALLOWED:
            self.out.append('<br>' if tag == 'br' else f'<{tag}>')

    def handle_startendtag(self, tag, attrs):
        if tag == 'br':
            self.out.append('<br>')

    def handle_endtag(self, tag):
        if tag in _ALLOWED and tag != 'br':
            self.out.append(f'</{tag}>')

    def handle_data(self, data):
        # Escape text and keep plain-text line breaks visible.
        self.out.append(escape(data).replace('\n', '<br>'))

    def result(self):
        return ''.join(self.out)


def sanitize_html(value):
    if not value:
        return ''
    p = _Sanitizer()
    p.feed(str(value))
    return p.result()


def safe_html(value):
    """Jinja filter: sanitize then mark safe so the whitelist tags render."""
    return Markup(sanitize_html(value))
