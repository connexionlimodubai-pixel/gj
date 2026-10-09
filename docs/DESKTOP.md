# The OpenBerry desktop app

The desktop app is OpenBerry packaged as a normal program. You download it, double-click it, and the
dashboard opens in its own window. You don't need a terminal or Python, and nothing is installed
system-wide. Your data stays on your computer.

- [Download](#download)
- Install it on [Windows](#windows), a [Mac](#mac) or [Linux](#linux)
- [Connect Claude Desktop](#connect-claude-desktop)
- [Your data and settings](#your-data-and-settings)
- [Updating](#updating) and [closing the app](#closing-the-app)
- [If something goes wrong](#if-something-goes-wrong)
- For developers: [building it yourself](#building-it-yourself) and [releasing a new version](#releasing-a-new-version)

## Download

Open the [Releases page](https://github.com/connexionlimodubai-pixel/gj/releases), click the newest
release, and under **Assets** download the file for your computer:

| Your computer | The file to download |
|---|---|
| Windows 10 or 11 | `OpenBerry-<version>-windows-x64.zip` |
| Mac with Apple silicon (M1 or newer), macOS 11 or later | `OpenBerry-<version>-macos-arm64.zip` |
| Linux, 64-bit | `OpenBerry-<version>-linux-x64.tar.gz` |

`<version>` is the release number, for example `OpenBerry-0.1.0-windows-x64.zip`.

There is no download for Macs with an Intel processor yet. To check which kind you have, open the
Apple menu and click **About This Mac**: "Chip: Apple M…" means Apple silicon.

**No release yet?** Every change to the app is also built automatically, and those builds are kept for 14 days.
1. Sign in to GitHub.
2. Open the repository's **Actions** tab and click **Desktop app**.
3. Click the newest run with a green tick.
4. Scroll down to **Artifacts** and download the one for your computer.

GitHub wraps each of these downloads in an extra zip file. Unzip it first to get the file in the table above.

## Windows

1. **Unzip it.** Right-click the downloaded `.zip` file, choose **Extract All…**, and pick a folder
   where the app can stay, such as your **Documents** folder. Don't run the app from inside the zip file.
2. **Open it.** Open the new `OpenBerry` folder and double-click **OpenBerry** (`OpenBerry.exe`).
3. **The first time**, Windows may show a blue box saying *"Windows protected your PC"*. Click
   **More info**, then **Run anyway**. Windows shows this box because the app isn't code-signed: a signing
   certificate costs money, and OpenBerry is free. The app is built by GitHub from the public source code.
4. OpenBerry opens in its own window.

Keep the files in the `OpenBerry` folder together. `openberry-cli.exe` and the `_internal` folder are part
of the app. To open the app more easily, right-click `OpenBerry.exe` and choose **Send to → Desktop
(create shortcut)**, or pin it to the taskbar once it is open.

The window uses Microsoft Edge WebView2, which Windows 10 and 11 already include. If it is missing
(this is rare, mostly on old Windows 10 PCs), OpenBerry opens in your web browser instead. Install the free
[WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) from Microsoft to get the window.

## Mac

1. **Unzip it.** Double-click the downloaded `.zip` file. Safari often unzips it for you.
2. **Move it to Applications.** Drag **OpenBerry** into your **Applications** folder. Do this before you
   connect Claude, because Claude remembers where the app is.
3. **The first time**, macOS blocks the app because it isn't notarised by Apple. Notarisation needs a paid Apple
   developer account. To open it anyway:
   - **macOS 15 (Sequoia) or later:**
     1. Double-click OpenBerry. When macOS says it can't open it, click **Done**.
     2. Open **System Settings → Privacy & Security** and scroll down.
     3. Next to *"OpenBerry" was blocked*, click **Open Anyway**.
     4. In the box that appears, click **Open Anyway** again, and enter your password if macOS asks for it.
   - **macOS 14 (Sonoma) or earlier:** in Applications, right-click (or Control-click) OpenBerry, choose
     **Open**, then click **Open** again.

   After the first time, OpenBerry opens normally. macOS shows this warning because the app isn't notarised,
   not because something is wrong with it: the app is built by GitHub from the public source code.

## Linux

1. Extract it: right-click the `.tar.gz` file and choose **Extract Here**, or run
   `tar -xzf OpenBerry-*-linux-x64.tar.gz`.
2. Run `./OpenBerry/OpenBerry` from a terminal. The dashboard opens in your web browser. The Linux app has
   no window of its own: that would need system libraries (GTK or Qt) that differ between Linux distributions.
3. To stop OpenBerry, press **Ctrl+C** in that terminal. If you started it by double-clicking, there is no
   Quit button: run `pkill -f OpenBerry/OpenBerry`, or end it in your system monitor.

## Using the app

- The first time, choose **Load demo data** to look around with made-up leads, or **Register your company**
  to start for real.
- The dashboard runs on your computer at `http://127.0.0.1:8000`, or at the next free number up to 8099
  if 8000 is taken. Only your own computer can open it.
- If you open the app while it is already running, it doesn't start a second copy. On a Mac it brings
  OpenBerry to the front; on Windows it opens another window onto the same dashboard.

## Connect Claude Desktop

Claude does the research and writing. It uses OpenBerry's tools through the program `openberry-cli`, which
comes with the app.

1. In OpenBerry, click **Connect Claude** in the menu.
2. In the **Claude Desktop** box, click **Copy**. The copied settings already point at the app on
   your computer.
3. In Claude Desktop, open **Settings → Developer → Edit Config**. This opens a file called
   `claude_desktop_config.json`.
   - If the file is empty or only contains `{}`, replace everything in it with what you copied.
   - If it already has an `"mcpServers"` section, add the `"openberry": { … }` part inside that section,
     with a comma between it and the entry before it.
4. Save the file, quit Claude Desktop completely, then open it again. On Windows, right-click Claude's
   icon next to the clock and choose **Quit**. On a Mac, use **Claude → Quit Claude**.
5. Ask Claude: *"Use openberry: list my companies."*

The settings look like this. The paths are examples, and your own are in the **Connect Claude** page:

```json
{
  "mcpServers": {
    "openberry": {
      "command": "/Applications/OpenBerry.app/Contents/MacOS/openberry-cli",
      "args": ["mcp"],
      "env": { "OPENBERRY_DB": "/Users/you/.openberry/openberry.db" }
    }
  }
}
```

On Windows the command is like `C:\\Users\\you\\Documents\\OpenBerry\\openberry-cli.exe`: in this file every
`\` is written twice. The copied settings already do that for you.

- **When OpenBerry is closed,** Claude can still use its tools: Claude starts `openberry-cli` in the
  background, and it uses the same data. The links in Claude's answers only open while the app is open.
- **If you move the app** to another folder, open **Connect Claude** again and copy the new settings
  into Claude Desktop.
- **A yellow warning on the Connect Claude page** means OpenBerry is running from a temporary place: on a Mac,
  straight from the Downloads folder instead of Applications; on Windows, from inside the zip file. Claude
  wouldn't find it there later. Do what the warning says, then copy the settings.

## Your data and settings

Everything OpenBerry stores is in a folder named `.openberry` in your home folder:

| Computer | Folder |
|---|---|
| Windows | `C:\Users\<you>\.openberry` |
| Mac | `/Users/<you>/.openberry`. The folder is hidden: in Finder, press **Cmd+Shift+G** and type `~/.openberry` |
| Linux | `~/.openberry` |

Inside the folder:

- **`openberry.db`** holds all your companies, leads, signals and messages. To back up, copy this file while
  OpenBerry is closed.
- **`.env`** holds your settings, if you have any (see below).
- **`logs/desktop.log`** records what the app did. Look here, or send it along, when something goes wrong.
- **`webview/`** and **`desktop-secret-key`** keep you logged in to the window between runs.

**Settings.** OpenBerry works without any settings. For optional features, create a plain-text file called
`.env` in the `.openberry` folder with one `NAME=value` per line. For example:

```
OPENBERRY_CONTACT_EMAIL=you@example.com
GITHUB_TOKEN=ghp_your_token
```

Then close OpenBerry and open it again. The [README](../README.md#configuration) and
[`.env.example`](../.env.example) list every setting.

## Updating

1. Close OpenBerry.
2. Download the new version.
3. Replace the old app with it:
   - **Windows:** delete the old `OpenBerry` folder, then extract the new one in the same place.
   - **Mac:** drag the new OpenBerry into Applications and choose **Replace**.
   - **Linux:** extract the new version over the old one.
4. The first time you open the new version, Windows or macOS asks you to confirm again, as when you installed it.

Your data in `.openberry` stays as it is. If you put the new version in a different place, copy the
Claude Desktop settings again ([Connect Claude Desktop](#connect-claude-desktop)).

## Closing the app

Closing the window stops OpenBerry (on a Mac, **OpenBerry → Quit OpenBerry** or Cmd+Q also works). Its
scheduled scans and hot-lead alerts only run while it is open. When you open it again, any scans that have
fallen due run straight away. Claude Desktop can still use OpenBerry's tools while the app is closed.

If OpenBerry opened in your web browser instead of its own window (see below), closing the browser tab does
not stop it: it keeps running in the background. Restart your computer to stop it, or end **OpenBerry** in
Task Manager (Windows: Ctrl+Shift+Esc).

## If something goes wrong

- **The app doesn't open, or closes straight away.** Read `.openberry/logs/desktop.log`. The last lines say why.
- **Windows: it opens in the browser instead of a window.** Install the
  [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
- **Windows: "Smart App Control blocked an app that may be unsafe".** There is no *Run anyway* button for
  this one. Smart App Control (on some new Windows 11 PCs) only allows signed apps, and OpenBerry isn't signed
  yet. You can turn it off in **Windows Security → App & browser control → Smart App Control settings**; that is
  your decision, and it affects every app.
- **Windows: your antivirus removed `OpenBerry.exe` or `openberry-cli.exe`.** Some antivirus programs distrust
  new, unsigned programs. Restore the file from the antivirus's quarantine and mark it as allowed, or extract
  the zip again.
- **Mac: "OpenBerry is damaged and can't be opened".** The download was probably unzipped by another
  program. Download it again and double-click the zip file in Finder. If that doesn't help, open
  **Terminal** and run `xattr -dr com.apple.quarantine /Applications/OpenBerry.app`.
- **Claude doesn't show the openberry tools.**
  1. Check that the app is still where the settings say.
  2. Copy the settings again from **Connect Claude**.
  3. Quit Claude Desktop completely and reopen it.
  4. On a Mac, if a message says *"openberry-cli" Not Opened* (or can't be opened), allow it the same way as
     the app: **System Settings → Privacy & Security → Open Anyway**, then restart Claude Desktop. Or run the
     `xattr` command from the "damaged" item above, which allows everything inside OpenBerry at once.

  Claude Desktop's own log is under **Settings → Developer → Open Logs Folder**, in `mcp-server-openberry.log`.

## Building it yourself

The app is built with [PyInstaller](https://pyinstaller.org). Build on the system you are building for:
PyInstaller can't make a Windows app on a Mac, for example. You need [uv](https://docs.astral.sh/uv/) and a
copy of this repository.

```bash
uv sync --extra desktop                     # on Linux: uv sync (no window there)
uv pip install pyinstaller pyinstaller-hooks-contrib
uv run --no-sync python packaging/build.py  # build the app and its download
uv run --no-sync python packaging/smoke_test.py dist/OpenBerry/openberry-cli dist/OpenBerry/OpenBerry
```

On a Mac, test `dist/OpenBerry.app/Contents/MacOS/openberry-cli` and `dist/OpenBerry.app/Contents/MacOS/OpenBerry`.
On Windows, add `.exe` to both names.

What the build makes in `dist/`:

| | Windows | Mac | Linux |
|---|---|---|---|
| The app | `OpenBerry/OpenBerry.exe` | `OpenBerry.app` | `OpenBerry/OpenBerry` |
| Command line (Claude Desktop runs `openberry-cli mcp`) | `OpenBerry/openberry-cli.exe` | `OpenBerry.app/Contents/MacOS/openberry-cli` | `OpenBerry/openberry-cli` |
| The download | `OpenBerry-<version>-windows-x64.zip` | `OpenBerry-<version>-macos-arm64.zip` | `OpenBerry-<version>-linux-x64.tar.gz` |

How it fits together:

- **`packaging/openberry.spec`** is the PyInstaller recipe. It builds two executables from one analysis of
  `packaging/launcher.py`, and they share the `_internal` folder:
  - `OpenBerry` has no console.
  - `openberry-cli` has a console, for Claude Desktop's stdio and for every `openberry` command
    (`openberry-cli serve`, `openberry-cli demo`, …).

  Both run `openberry.desktop.main`. On a Mac the spec wraps the folder into `OpenBerry.app`
  (`io.openberry.desktop`), with both executables in `Contents/MacOS`. On Linux it leaves out pywebview.
- **`packaging/build.py`** runs PyInstaller, then packs the download. On a Mac it uses `ditto`, which keeps
  the app's symlinks and signature. Its options:
  - `--skip-build` only repacks.
  - `--no-archive` only builds.
  - `--release-tag vX.Y.Z` checks the version.
- **`packaging/smoke_test.py`** checks a build with a temporary data folder:
  1. `--version`
  2. `demo`
  3. `serve`: health check, the demo dashboard, the Connect Claude page and a static file.
  4. An MCP handshake over stdio with `tools/list` (21 tools) and a tool call.
  5. `OpenBerry --smoke`: the app starts its server, checks it and stops, without a window.

  It exits with an error and says what broke.
- **`packaging/icons/`** holds the app icons. `packaging/make_icons.py` makes them from the dashboard's logo.
- On Linux the unpacked app is about 86 MB and the download about 43 MB.

To run the app from source instead, use `uv run openberry desktop`, after `uv sync --extra desktop` for the window.

## Releasing a new version

1. Set the same new version in `src/openberry/__init__.py` (`__version__`) and in `pyproject.toml`. Then run
   `uv lock` and merge the change into `main`.
2. Tag the release and push the tag:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
3. The **Desktop app** workflow (`.github/workflows/desktop.yml`) then:
   1. Builds the app on Windows, macOS (Apple silicon) and Linux.
   2. Smoke-tests each build.
   3. Attaches the three downloads to a GitHub Release named after the tag, with notes generated from the merged changes.

   If the tag doesn't match the version in the code, the build stops before anything is published.

Pull requests and pushes to `main` that change the app run the same build and smoke test. Their downloads stay
under the run's **Artifacts** for 14 days.

**Not done yet:**
- Code signing (Windows) and notarisation (macOS). Until then, people see the warnings described above.
- An Intel Mac build. Add an Intel macOS runner (`macos-13`) to the workflow's matrix to make one.
- Automatic updates.
