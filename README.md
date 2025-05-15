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
> As of 2024-11-22 or so, YouTube started denying the Android client from making requests that
> obtain a DASH manifest.  As a result, `moonarchive` currently only issues requests as the web
> client.
> 
> See the section on [Proof-of-origin downloads](#proof-of-origin-downloads) if you intend to
> download streams in parallel.

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

1. `pip install "moonarchive[keepawake] @ git+https://github.com/nosoop/moonarchive"`
    - `keepawake` is an optional extra.  See the [Extras](#extras) section below for further
    details on each.
2. At minimum, `moonarchive ${URL}` on an upcoming or currently live stream to download;
`moonarchive --help` to view all possible options.

For development:

1. Use `[keepawake,dev]` with the `pip` command above to install all packages, including those
for development.
2. Install [`just`](https://github.com/casey/just).
3. Make your changes.  Prior to committing, run `just test` and `just format`.

### via uv

[`uv`][] is an alternative package manager for Python.  The benefit of using `uv` is that it'll
manage installing Python as needed and isolating the library dependencies for you.

1. [Install `uv`.](https://docs.astral.sh/uv/getting-started/installation/)
2. `uv python install 3.11` to install Python 3.11.  Newer versions may work.
3. `uv tool install "moonarchive[keepawake] @ git+https://github.com/nosoop/moonarchive"`
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

#### `dev`

Installs `mypy` and `ruff` for typechecking and linting respectively.  This ensures
consistency of code formatting for contributors, and tries to check the correctness of type
annotations.

## Features

- Detection of split broadcasts (updated manifests).  See [ytarchive#56][].  tl;dr in certain
situations, ytarchive will appear to stall if the streamer changes certain settings mid-stream.
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

Important note on cookie authentication:  YouTube frequently rotates cookies; while a given file
will work during the start of a stream, it will need to be updated whenever a player request is
made (since you're authenticated with proof-of-origin, this happens every 6 hours, or if the
stream goes offline and needs to be rechecked).

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

Pass one of the following:

- `--po-token ${POTOKEN} --visitor-data ${VISITOR_DATA}` for non-logged in contexts
- `--po-token ${POTOKEN} --cookies ${COOKIE_FILE}` for logged in contexts (member streams, etc.)

It's not really clear how often you need to obtain a new proof-of-origin token.  The linked
guide says 12 hours, but my personal experience on a residential connection has both visitor
data and guest cookies lasting more than a month with their corresponding tokens.

It's likely that tokens linked to an actual user will be rotated out sooner, though I haven't
been able to test that myself.

[obtain a token]: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide

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
