# moonarchive

A Python module and tool to download ongoing YouTube streams in its entirety.

Shout outs to [`ytarchive`][] and [`yt-dlp`][] for their efforts in the space.  They get all the
credit for documenting the download process; I'm only working to improve parts of that.

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

## Installation

If you're comfortable with Python, it's probably a good idea to install this in a virtual
environment so the libraries are isolated from the rest of the system.

1. `pip install git+https://github.com/nosoop/moonarchive#egg=moonarchive[keepawake]`
    - Using `[keepawake]` installs an optional library that ensures the system doesn't go into
    standby while downloading.
2. At minimum, `moonarchive ${URL}` on an upcoming or currently live stream to download;
`moonarchive --help` to view all possible options.

For development:

1. Use `[keepawake,dev]` with the `pip` command above to install all packages, including those
for development.
2. Install [`just`](https://github.com/casey/just).
3. Make your changes.  Prior to committing; run `just test` and `just format`.

## Dependencies

- Python 3.11 or newer is required.  The installation will bring in a number of other
third-party packages.
- [ffmpeg][] is required for creating the final file.  This is not installed automatically.
On Windows, you can use `winget install ffmpeg`; for other platforms refer to your package
manager.

[ffmpeg]: https://ffmpeg.org/download.html

## Features

- Detection of updated manifests.  See [ytarchive#56][].  tl;dr in certain situations,
ytarchive will appear to stall if the streamer changes certain settings mid-stream.

[ytarchive#56]: https://github.com/Kethsar/ytarchive/issues/56

## Contributions

I wrote this tool mainly for myself, so issues / pull requests that are opened will likely be
ignored as I don't have the capacity to maintain it for other peoples' use cases.  Sorry!

However, feel free to open both regardless &mdash; issue trackers are meant to serve the
end users as much as they serve the project owner(s).  Maybe someone else will see them and
would be willing to maintain the project or lift the important parts of the code for use in
better-maintained projects.  The less work I have to do, the better!

If you are interested in opening a pull request with the sole purpose of getting it merged,
please poke me with an issue prior to be certain that I'm interested in taking it.

## License

Released under the Zero-Clause BSD license.
