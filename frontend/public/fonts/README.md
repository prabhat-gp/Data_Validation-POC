Put the PP Telegraf web fonts here, with exactly these names:

    PPTelegraf-Light.woff2      (300)
    PPTelegraf-Regular.woff2    (400)
    PPTelegraf-Medium.woff2     (500)
    PPTelegraf-Bold.woff2       (700)

That is all. app/globals.css already declares them and lists PP Telegraf
first in the stack, so it takes over on the next page reload.

Missing weights fall through to the next font. Until the files are here the
app renders in the OS sans-serif.
