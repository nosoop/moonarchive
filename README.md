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
    - Using `[keepawake]` installs an optional library that ensures the system doesn't go into
    standby while waiting for the stream and while downloading.  Otherwise, you will need to
    pass `--no-keep-awake` to the application to acknowledge that possibility.
    - The keepawake mechanism only applies to the CLI application; if you are calling the API
    directly you will have to replicate it yourself.
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

## Features

- Detection of updated manifests.  See [ytarchive#56][].  tl;dr in certain situations,
ytarchive will appear to stall if the streamer changes certain settings mid-stream.
- Handling of landscape-and-portrait transitions.  A streamer may swap width and height &mdash;
this does not generate a new manifest, and resulting naively muxed files end up being garbage.

Important note on cookie authentication:  YouTube frequently rotates cookies; while a given file
will work during the start of a stream, you will need an up-to-date copy whenever a player
request is made.

[ytarchive#56]: https://github.com/Kethsar/ytarchive/issues/56

### Proof-of-origin downloads

YouTube has started enforcing the use of proof-of-origin to verify that requests are made from a
real browser environment.

Without it, streams will expire after 30 seconds and `moonarchive` will need to make repeated
requests for new copies of the manifest.  This behavior, when done at a high enough frequency,
will trigger YouTube's bot detection, which at minimum will invalidate ongoing downloads.

While one instance of `moonarchive` should not make requests frequently enough to trigger
YouTube, if you plan on downloading items in parallel it's strongly recommended to
[obtain a token][].

Pass one of the following:

- `--po-token ${POTOKEN} --visitor-data ${VISITOR_DATA}` for non-logged in contexts
- `--po-token ${POTOKEN} --cookies ${COOKIE_FILE}` for logged in contexts (member streams, etc.)

[obtain a token]: https://github.com/yt-dlp/yt-dlp/wiki/Extractors#po-token-guide

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
