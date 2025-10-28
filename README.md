# moonarchive

A Python module and tool to download ongoing YouTube streams in its entirety.

Shout outs to [`ytarchive`][] and [`yt-dlp`][] for their efforts in the space.  They get all the
credit for documenting the download process; I'm only working to improve parts of that.

Also to [glubsy/livestream_saver][], though I saw that long after the above.

(If I knew Go or was comfortable parsing out yt-dlp's live-from-start logic, I'd be contributing
changes to those projects instead.)

> [!WARNING]
> 
> You should probably not use this tool as your only solution for downloading livestreams
> unsupervised; the author recommends running one of the other projects listed above in parallel
> or at least actively watching the progress to ensure that the tool is behaving correctly.

> [!NOTE]
> 
> The module API design is currently in flux.  Unless you want to keep up with changes, maybe
> don't import the module and call the downloading logic yourself for now.  The command-line
> frontend probably won't change too much in comparison, but do review the help text after
> upgrading.

[`ytarchive`]: https://github.com/Kethsar/ytarchive
[`yt-dlp`]: https://github.com/yt-dlp/yt-dlp
[glubsy/livestream_saver]: https://github.com/glubsy/livestream_saver

## Installation

> [!WARNING]
> To minimize the risk of your connection being flagged, see the section on
> [Proof-of-origin downloads](#proof-of-origin-downloads).

(Looking for a graphical interface to manage things?  Check out [moombox][].)

[moombox]: https://github.com/nosoop/moombox

### Dependencies

- `moonarchive` is tested against Python 3.11.  The installation will bring in a number of other
third-party packages.
- [ffmpeg][] is required for creating the final file.  This is not installed automatically.
On Windows, you can use `winget install ffmpeg`; for other platforms refer to your package
manager.

[ffmpeg]: https://ffmpeg.org/download.html

### via pip

If you're comfortable with Python, it's probably a good idea to install this in a virtual
environment so the libraries are isolated from the rest of the system.

If you're not, consider [installing via uv](#via-uv) instead.

1. `pip install "moonarchive[keepawake,cookies] @ git+https://github.com/nosoop/moonarchive"`
    - `keepawake` and `cookies` are optional extras.  See the [Extras](#extras) section below
    for further details on each.
2. At minimum, `moonarchive ${URL}` on an upcoming or currently live stream to download;
`moonarchive --help` to view all possible options.

For development:

1. Use `[keepawake,cookies,dev]` with the `pip` command above to install all packages, including
those for development.
2. Install [`just`](https://github.com/casey/just).
3. Make your changes.  Prior to committing, run `just test` and `just format`.

### via uv

[`uv`][] is an alternative package manager for Python.  The benefit of using `uv` is that it'll
manage installing Python as needed and isolating the library dependencies for you.

1. [Install `uv`.](https://docs.astral.sh/uv/getting-started/installation/)
2. `uv python install 3.11` to install Python 3.11.  Newer versions may work.
3. `uv tool install "moonarchive[keepawake,cookies] @ git+https://github.com/nosoop/moonarchive"`
4. `moonarchive ${URL}` (you may need to `uv tool update-shell` or `source $HOME/.local/bin/env`
to update your `PATH` environment variable)

[`uv`]: https://docs.astral.sh/uv/

### Extras

The package includes a number of optional dependencies that can be installed for additional
functionality.

#### `keepawake`

This installs an optional dependency ensuring that, when using the CLI, the system doesn't
automatically go into standby while the application is running (waiting for a stream and while
downloading).

If this extra is not installed, you will need to pass `--no-keep-awake` to the application to
acknowledge the possibility that the system may sleep.

In the event that the system does go to sleep (whether manually or on idle) moonarchive should
gracefully recover, provided the stream contents are still available.

The keepawake behavior only applies to the CLI application and is not active when using the
module API.

#### `cookies`

This installs an optional dependency on [browser-cookie3][], allowing moonarchive to access the
browser's cookies on-demand whenever certain requests are made.

With this extra, you can use `--cookies-from-browser` to specify a given browser to extract
cookies from, and `--cookies` to specify the cookie database for that browser.

Without this extra, you can still use `--cookies` to specify a cookie file in Netscape format,
which will also be loaded as needed on requests.  It's recommended to follow
[the yt-dlp guide on exporting YouTube cookies][yt-dlp cookies] in that case to ensure cookies
are valid across the lifetime of the stream download.

I've personally checked the code in [the 0.20.1 release][audited bc3] and found nothing
questionable.  Whether or not that's acceptable is up to you to decide.

[browser-cookie3]: https://pypi.org/project/browser-cookie3
[audited bc3]: https://github.com/borisbabic/browser_cookie3/commit/03895797e48dd107806db171d8392c562151807d
[yt-dlp cookies]: https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies

#### `dev`

Installs `mypy` and `ruff` for typechecking and linting respectively.  This ensures
consistency of code formatting for contributors, and tries to check the correctness of type
annotations.

## Features

- Detection of split broadcasts (updated manifests).  See [ytarchive#56][].  tl;dr in certain
situations, ytarchive will not reset its sequence counter when attempting to retrieve fragments
on a new broadcast, appearing to stall.
    - moonarchive, in its current form, will mux out an individual file per broadcast (assuming
    no other broadcast issues).  In theory it's possible to precisely merge broadcasts to one
    file, but this requires processing the raw file's internal segment metadata added by YouTube
    plus potential reencoding due to differing resolutions and codecs.
    - As far as I'm aware, regardless of this functionality, you cannot obtain fragments from a
    previous broadcast if you start the application late.
- Handling of landscape-and-portrait transitions.  A streamer may swap width and height &mdash;
this does not generate a new manifest, and resulting naively muxed files end up being garbage.
    - moonarchive will not merge streams affected by this, and it's left to the user to decide
    how to process the result (video streams will be split at resolution change boundaries,
    while the audio streams will span the length of the broadcast).

[ytarchive#56]: https://github.com/Kethsar/ytarchive/issues/56

### Proof-of-origin downloads

YouTube has started enforcing the use of proof-of-origin to verify that requests are made from a
real browser environment.

Without it, streams will expire after 30 seconds and `moonarchive` will need to make repeated
requests for new copies of the manifest.  This behavior, when done at a high enough frequency,
will trigger YouTube's bot detection, which at minimum will invalidate ongoing downloads.

If `moonarchive` needs to obtain a new copy of the manifest, the following message is displayed
in the application output:

> Received HTTP 403 error cur_seq=21605, getting new player response

(As stated before, with proof-of-origin, manifests normally expire after 6 hours.  Seeing this
message on long-running streams is expected behavior.)

While one instance of `moonarchive` should not make requests frequently enough to trigger
YouTube, if you plan on downloading items in parallel it's strongly recommended to
[obtain a token][].

> [!WARNING]
> The below information is somewhat outdated due to tests being rolled out by YouTube and will
> be removed once the application is stabilized.  Please skip to to the section on
> [content-based tokens](#content-based-tokens) for the current changes.

Pass one of the following (where `${VAR}` is variable information):

- `--po-token ${POTOKEN} --visitor-data ${VISITOR_DATA}` for non-logged in contexts
- `--po-token ${POTOKEN} --cookies ${COOKIE_FILE}` for logged in contexts (member streams, etc.)

It's not really clear how often you need to obtain a new proof-of-origin token.  The linked
guide says 12 hours, but my personal experience on a residential connection has visitor data
remaining valid for 180 days, with their respective tokens working for the same amount of time.
(Once the visitor data expires, no tokens generated from that information will work, with
YouTube behaving as if the token was not present at all.)

It's likely that tokens linked to an actual user will be rotated out sooner, though I haven't
been able to test that myself.

Proof-of-origin is very much an evolving thing, so my personal observations at a given time may
differ from that of others.

[obtain a token]: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide

#### Content-based tokens

As of around 2025-10-14, YouTube started broadly rolling out a change where the video server
(GVS) now accepts tokens generated based on video ID.

This effectively makes token generation occur at every download, instead of on the order of
months.

In v0.4.4, `moonarchive` is able to leverage
[Brainicism's proof-of-origin token provider][bgutil-pot], the same software currently
recommended by yt-dlp's maintainers.  To use this, pass the URL of the instance:

```
--unstable-bgutil-pot-provider-url "http://127.0.0.1:4416"
```

Note that this option is currently experimental and subject to change once the API stabilizes.

[bgutil-pot]: https://github.com/Brainicism/bgutil-ytdlp-pot-provider

## Contributions

I designed this tool around my specific needs, so issues / pull requests that are opened may
be ignored as I don't have the capacity to maintain it for other peoples' use cases.  Sorry!

However, feel free to open both regardless &mdash; issue trackers are meant to serve the
end users as much as they serve the project owner(s).  Maybe someone else will see them and
would be willing to maintain the project or lift the important parts of the code for use in
better-maintained projects.  The less work I have to do, the better!

If you are interested in opening a pull request with the sole purpose of getting it merged,
please poke me with an issue beforehand to confirm that I'm both interested in taking it and
not already working on something similar.

## License

All code in this repository is released under the Zero-Clause BSD license.  That said, please
also review the dependencies' licenses to ensure compliance with them as well should you be
distributing the code.
