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
| `nerd-symbols.woff2` | Nerd Fonts `SymbolsNerdFontMono-Regular` | v3.2.1 (subset) |

Fetched from `cdn.jsdelivr.net/npm/...`. To update, re-fetch the same
paths at a new version and update this table. These are build outputs,
not sources — do not hand-edit them.

## nerd-symbols.woff2

The tmux Tokyo Night status bar draws powerline separators and a few
icons from the Nerd Font private-use area. Without them the browser
renders tofu boxes.

The full symbols font is 2.1 MB, which is not worth vendoring. This is a
**subset containing only the glyphs the theme actually uses** — 944
bytes. The set was derived by rendering the theme's `status-left`,
`window-status-current-format` and `status-right` and collecting every
non-ASCII codepoint:

    U+2735  ✵     pane-synchronized marker
    U+E0B0        powerline right separator
    U+E0D7        powerline separator (inverse)
    U+EA85        window icon
    U+EB81        window icon
    U+F11C        keyboard icon

U+E0B1..U+E0B3 are included too, since they are the other three
powerline separators and cost nothing.

To regenerate after a theme change, re-collect the codepoints and:

    pyftsubset SymbolsNerdFontMono-Regular.ttf \
      --unicodes=U+2735,U+E0B0,U+E0B1,U+E0B2,U+E0B3,U+E0D7,U+EA85,U+EB81,U+F11C \
      --flavor=woff2 --output-file=nerd-symbols.woff2 --no-hinting --desubroutinize
