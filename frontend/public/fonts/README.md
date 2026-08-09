# Brand font — PP Telegraf

honeywellaerospace.com uses **PP Telegraf** (Pangram Pangram). It is a
commercial licence Honeywell holds; it cannot be downloaded or bundled by
anyone else, which is why this folder ships empty.

## To use the real font

Get the web files from the Honeywell brand portal / design team and drop them
here with these EXACT names:

    public/fonts/PPTelegraf-Light.woff2      (300)
    public/fonts/PPTelegraf-Regular.woff2    (400)
    public/fonts/PPTelegraf-Medium.woff2     (500)
    public/fonts/PPTelegraf-Bold.woff2       (700)

Nothing else to change. `globals.css` already declares @font-face for all four
and lists PP Telegraf first in the stack, so it takes over on the next reload.
Any weight you don't have simply falls through to the next font.

`.woff` or `.ttf` instead of `.woff2` also work — edit the `src:` lines in the
@font-face block at the top of globals.css.

## Until then

The stack falls back to the OS sans-serif. That is not the brand face and does
not pretend to be.
