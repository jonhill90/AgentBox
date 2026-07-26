# Vendored assets

xterm.js and its fit addon, served by `/ui` for the terminal panel
(SPEC §15.5).

Vendored rather than loaded from a CDN on purpose: SPEC §6 fixed `/ui`
as one static page with no build step, and AgentBox is a local-only box
that must work with no outbound network. A CDN would break both.

| File | Source | Version |
|---|---|---|
| `xterm.js` | `@xterm/xterm` `lib/xterm.js` | 5.5.0 |
| `xterm.css` | `@xterm/xterm` `css/xterm.css` | 5.5.0 |
| `xterm-addon-fit.js` | `@xterm/addon-fit` `lib/addon-fit.js` | 0.10.0 |

Fetched from `cdn.jsdelivr.net/npm/...`. To update, re-fetch the same
paths at a new version and update this table. These are build outputs,
not sources — do not hand-edit them.

## Fonts are NOT vendored

The tmux Tokyo Night status bar needs Nerd Font glyphs (powerline
separators, window icons). Those come from **FiraCode Nerd Font**, which
is installed in the image (see Dockerfile) and on the operator's machine
— the same arrangement hill90-app uses, where `XTerminal.tsx` simply
names the font and lets the OS supply it.

A previous version vendored a ~1KB subset of SymbolsNerdFontMono here.
That was a mistake twice over: the Mono symbols variant has no outlines
for U+E0B4/U+E0B6 (the round separators the config actually uses), and
because the subset was listed FIRST in the font stack it claimed those
codepoints and rendered them blank instead of falling through to the
real font. Do not reintroduce it.
