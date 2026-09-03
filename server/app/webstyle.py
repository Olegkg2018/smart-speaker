"""Общий вид служебных страниц (`/` и `/satellite`).

Обе — одностраничники на ванильном HTML без сборщика (см. webui.py,
satellite_page.py): вставлять внешний фреймворк ради этого дороже, чем
польза. Но общие токены и навигацию держать в двух копиях означало бы
чинить один и тот же цвет в двух файлах — вынесено сюда, а страницы
подставляют это через `.replace()` (простой текст, без сборки и рисков
конфликта с фигурными скобками в их собственном JS).
"""

from __future__ import annotations

BASE_CSS = """
  :root {
    color-scheme: light dark;
    --bg: #0a0c12; --surface: #12151f; --surface2: #191d2c;
    --line: #ffffff17; --text: #edeff5; --muted: #8990a3;
    --accent: #7c6cff; --accent2: #4f46e5; --good: #3ecf8e;
    --danger: #ff6b7a; --radius: 18px; --radius-sm: 12px;
    --shadow: 0 12px 32px -8px #0009;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f2f3f9; --surface: #ffffff; --surface2: #f5f6fb;
      --line: #10132412; --text: #14172a; --muted: #666e88;
      --shadow: 0 12px 28px -10px #14172a22;
    }
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background:
      radial-gradient(1100px 480px at 50% -180px, rgba(124,108,255,.16), transparent),
      var(--bg);
    color: var(--text);
    font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
    min-height: 100vh;
  }
  a { color: inherit; }
  .wrap { max-width: 720px; margin: 0 auto; padding: 0 20px 64px; }

  nav.top { max-width: 720px; margin: 0 auto; padding: 20px 20px 4px;
            display: flex; align-items: center; justify-content: space-between; }
  nav.top .brand { display: flex; align-items: center; gap: 9px; font-weight: 800;
                   font-size: 1.02rem; letter-spacing: .2px; }
  nav.top .brand .dot { width: 9px; height: 9px; border-radius: 50%;
                         background: var(--good); box-shadow: 0 0 0 4px rgba(62,207,142,.22); }
  nav.top .links { display: flex; gap: 4px; background: var(--surface);
                    border: 1px solid var(--line); padding: 4px; border-radius: 999px;
                    box-shadow: var(--shadow); }
  nav.top a { text-decoration: none; color: var(--muted); font-size: .84rem; font-weight: 700;
              padding: 7px 15px; border-radius: 999px; transition: color .15s, background .15s; }
  nav.top a:hover { color: var(--text); }
  nav.top a.here { background: linear-gradient(135deg, var(--accent), var(--accent2));
                    color: #fff; }

  .hero { padding: 28px 4px 6px; }
  .hero h1 { font-size: 1.7rem; margin: 0 0 6px; letter-spacing: -.01em; }
  .hero p { color: var(--muted); margin: 0; font-size: .94rem; }

  .card { background: var(--surface); border: 1px solid var(--line);
          border-radius: var(--radius); box-shadow: var(--shadow);
          padding: 18px 20px; margin-top: 16px; }
  .card h2 { font-size: .78rem; text-transform: uppercase; letter-spacing: .07em;
             color: var(--muted); margin: 0 0 4px; display: flex; align-items: center; gap: 8px; }
  .card .hint { color: var(--muted); font-size: .87rem; margin: 0 0 12px; }
  .card + .card { margin-top: 14px; }

  table { width: 100%; border-collapse: collapse; }
  td { padding: 10px 4px; border-bottom: 1px solid var(--line); vertical-align: top; }
  tr:last-child td { border-bottom: 0; }
  td.act { width: 1%; white-space: nowrap; text-align: right; }
  .when { font-variant-numeric: tabular-nums; font-weight: 700; }

  button, .btn { font: inherit; cursor: pointer; border: 1px solid var(--line);
           background: var(--surface2); color: var(--text); border-radius: 10px;
           padding: 7px 14px; font-weight: 600; font-size: .86rem; transition: .15s; }
  button:hover, .btn:hover { border-color: rgba(124,108,255,.55); }
  button:disabled { opacity: .5; cursor: default; }
  button.primary { background: linear-gradient(135deg, var(--accent), var(--accent2));
                    color: #fff; border: 0; }
  button.danger:hover { border-color: var(--danger); color: var(--danger); }

  .empty { color: var(--muted); font-style: italic; padding: 6px 4px; font-size: .9rem; }
  .sub { color: var(--muted); font-size: .85rem; font-weight: 400; }
  .card h3 { font-size: .92rem; margin: 4px 0 6px; }
  .badge { display: inline-block; background: var(--surface2); color: var(--muted);
           border-radius: 999px; padding: 2px 10px; font-size: .78rem; font-weight: 700; }
"""


def nav(active: str) -> str:
    def link(href: str, label: str, key: str) -> str:
        return f'<a class="{"here" if key == active else ""}" href="{href}">{label}</a>'

    return (
        '<nav class="top"><div class="brand"><span class="dot"></span>Happy Speaker</div>'
        '<div class="links">'
        + link("/", "Управление", "home")
        + link("/satellite", "Сателлит", "satellite")
        + "</div></nav>"
    )
