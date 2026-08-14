# Writing a theme

A theme replaces Steam's Home, game pages and loading screens with your own,
for as long as one is picked under **Custom Theme** in the plugin panel.

A theme is **one HTML file** and, if you want sound, a folder of audio next to
it. There is no build step and no toolchain: edit the file, reopen the panel,
look at it.

```
~/Documents/decky-links/themes/
    my-theme/
        theme.html
        sounds/
            insert.flac
```

Two directories are searched. Yours comes first, so copying a shipped theme out
and keeping its folder name is the cheapest way to tweak one.

| | |
| --- | --- |
| `~/Documents/decky-links/themes/` | Yours |
| The plugin's own `assets/themes/` | What ships |

The folder name is the theme's id: lower case, digits, `-` and `_`.

## theme.html

Three kinds of block, in one file:

```html
<script type="application/json" id="decky-theme">
{
  "name": "My Theme",
  "blurb": "One line for the picker",
  "sounds": { "scan": "insert.flac" },
  "scenes": {
    "reading": { "loopSound": "seek.flac", "minVisibleMs": 400 }
  }
}
</script>

<style>
  .screen { width: 100%; height: 100%; background: #101; color: #eee; }
</style>

<template data-scene="ready">
  <div class="screen">Present a tag</div>
</template>
```

The manifest carries the metadata, `<style>` carries the styling, and each
`<template data-scene="…">` is one screen. They live together so there is no
second file to keep in step with the first.

## Scenes

All optional. A theme that leaves one out falls back to its own `ready` screen
rather than letting Steam show through.

| Scene | When |
| --- | --- |
| `ready` | Waiting for a tag or disk. The screen a Deck sits on. |
| `ambient` | Nothing has happened for 90 seconds. |
| `reading` | A medium is being read. Often over in under 200 ms — see `minVisibleMs`. |
| `launching` | A game is starting. Ends when the game paints. |
| `error` | Unreadable medium, blocked URI, unrecognised key. |
| `locked` | Restricted mode, key absent. |

There is no scene for a game that is running. Once gamescope hands the screen
to a game, this layer is not on it — a "playing" screen would be a promise
nothing could keep. Sound still plays.

## Placeholders

Substituted wherever they appear in a screen:

| | |
| --- | --- |
| `{title}` | The game's name, when one is known |
| `{drive}` | `A` for a disk, `T` for a tag |
| `{time}` | The clock, `HH:MM` in the device's locale |

Values are escaped, so a game called `Command & Conquer` cannot close a tag.

`{time}` is the one that moves. A theme cannot run code, so a screen using it
is re-rendered every 20 seconds — but only on `ready`, `ambient` and `locked`,
the scenes a Deck sits in for hours. Re-rendering restarts any CSS animation in
the screen, which is why the scenes that pass in seconds keep the time they were
painted with: a progress bar that jumped back to the start every 20 seconds
would be worse than a clock that is a few seconds stale.

## Sound

Filenames in your `sounds/` folder. Any format the Steam client can decode —
`.flac`, `.wav` and `.ogg` are all safe.

```json
"sounds":  { "scan": "insert.flac", "error": "buzz.flac" },
"scenes":  { "reading": { "enterSound": "insert.flac", "loopSound": "seek.flac" } }
```

- **`sounds`** replaces the plugin's own event sounds: `scan`, `success`,
  `error`, `lock`, `unlock`.
- **`enterSound`** plays once when a scene starts.
- **`loopSound`** plays for as long as the scene lasts.

The difference matters. A latch closing happens *at* the moment a disk goes in,
so it is an `enterSound`. A drive seeking continues for as long as the drive is
working, so it is a `loopSound` — a one-shot would fall silent while the
progress bar on screen was still filling. Loops stop when a game starts.

**Watch for doubling.** Three of the plugin's events fire at the same instant a
scene begins: `scan` with `reading`, `error` with `error`, and `lock` with
`locked`. Giving those scenes an `enterSound` as well plays a second copy of the
same file a millisecond later — audible as a flam rather than as two sounds. If
a scene has an event, remap the event; `launching` and `ambient` have none,
which is why they are usually where a scene sound belongs.

## Per-scene options

| | |
| --- | --- |
| `minVisibleMs` | Wait this long before painting. For scenes that can pass in a blink: a tag reads in well under 200 ms, and a screen that appears for two frames reads as a fault rather than a load. Capped at 1.6 s. |
| `fadeMs` | Fade-in duration. Default 220 ms. |

Leave `minVisibleMs` off for `ready` and `ambient`. Those are held, and any
delay before them is a gap in which Steam shows through.

## What runs, and what does not

**Scripts do not run.** Screens are set as markup, so a `<script>` tag or an
`onclick` in a template does nothing at all. Animation is CSS: `@keyframes`,
`steps()`, `animation-delay`. Between them the shipped themes animate a blinking
block cursor, a segmented progress bar, a page hopping between folders, a
drifting starfield, a scrolling marquee, a coin falling into a slot, and a
chequerboard floor running to a horizon under a stack of copper bars — and not
one of them uses a line of JavaScript.

**Do not transform the outermost element.** A `scale`, `translate` or `rotate`
on a screen's root moves the only thing painting a background, and for as long
as it is off-centre or squeezed flat the rest of the layer has nothing in it.
The 16-bit theme's power-on effect did this and put Steam's library on screen
for a couple of hundred milliseconds in the middle of every launch. The layer
now paints black behind every theme, so the cost is a black gap rather than a
glimpse of Steam — but it still will not look like what you intended. Animate
something inside the root instead.

**Taps go nowhere.** The layer absorbs pointer and touch events, so touching a
theme cannot operate the Steam interface hidden underneath it. It used to let
them through, and the result was audible: tapping the screen played the click of
whichever Steam button happened to be behind that pixel. Your theme does not
have to do anything to get this — a screen with no buttons on it still cannot be
tapped through.

**Steam's menus stay on top.** The Quick Access and Steam menus render above any
theme, and the layer hides itself entirely while one is open, so the control
that switches this off is always both reachable and visible. Those buttons are
handled below the browser, so no theme can take them away.

## Testing yours

1. Put the folder in `~/Documents/decky-links/themes/`
2. Open the plugin panel — it re-reads the folder each time it opens
3. Pick your theme under **Custom Theme**

If it does not appear, the folder name is not a valid id or `theme.html` is
missing. If it appears but shows nothing, check that a template's `data-scene`
matches a name from the table above.

## Start from one of these

All four ship, all four use every feature described here, and they are
deliberately unalike — text mode with no boxes at all, a windowed desktop, a
cabinet, and a console boot screen. Between them they show that nothing above
was written around a particular look.

| | |
| --- | --- |
| [`assets/themes/dos/theme.html`](../assets/themes/dos/theme.html) | MS-DOS. Text mode, block cursor, drive that seeks. |
| [`assets/themes/desktop95/theme.html`](../assets/themes/desktop95/theme.html) | Desktop 95. Teal desktop, beveled windows, taskbar clock. |
| [`assets/themes/arcade/theme.html`](../assets/themes/arcade/theme.html) | Arcade. CRT bezel, coin doors, credit counter, attract mode. |
| [`assets/themes/console16/theme.html`](../assets/themes/console16/theme.html) | 16-Bit Console. Copper bars, a chequerboard horizon, a logo with an extrude. |

Their sounds are generated, not recorded — see
[`make-dos-sounds.py`](../scripts/make-dos-sounds.py),
[`make-desktop95-sounds.py`](../scripts/make-desktop95-sounds.py),
[`make-arcade-sounds.py`](../scripts/make-arcade-sounds.py) and
[`make-console16-sounds.py`](../scripts/make-console16-sounds.py) if you want a
set of your own. All are pure stdlib Python, take no arguments, and share the
small synthesiser in [`scripts/synth.py`](../scripts/synth.py).

## One thing to keep out

If you are building a theme after a real product, take the *era* and leave the
*trademarks*: no product names, no logos, no shipped font files, no sampled
system sounds. Desktop 95 and Arcade are both built as examples of that — a
Start button wearing this plugin's own mark instead of anyone's flag, a cabinet
booting an invented board on a Decky Links marquee, fonts named rather than
bundled, and every sound synthesised from scratch. A theme is something people
share, and it should be shareable.
